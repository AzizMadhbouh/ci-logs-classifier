import json
import os
import random
import re
import time
import urllib.error
import urllib.request

SEVERITY_LEVELS = ("Low", "Medium", "High", "Critical")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
PROVIDER = os.environ.get("SEVERITY_LLM_PROVIDER", "gemini")  # gemini | none

MAX_RETRIES = 6
RETRYABLE_CODES = {429, 500, 502, 503, 504}

# Free tier = 20 req/day per model; rotate so one exhausted quota doesn't block.
_MODEL_FALLBACKS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-3-flash-preview",
]

_POLICY_PATH = os.path.join(os.path.dirname(__file__), "severity_policy.json")
_POLICY = json.load(open(_POLICY_PATH, encoding="utf-8"))
_ROOT_CAUSE_BASELINES = _POLICY["root_cause_baselines"]
_PYLINT_MAP = _POLICY["tool_codes"]["pylint"]
_FLAKE8_MAP = _POLICY["tool_codes"]["flake8"]
_DOWNGRADE_SIGNALS = tuple(_POLICY["signals"]["downgrade"].keys())
_UPGRADE_SIGNALS = tuple(_POLICY["signals"]["upgrade"].keys())
_SEVERITY_RANK = {"Low": 0, "Medium": 1, "High": 2, "Critical": 3}

_BANDIT_SEV = re.compile(r"Severity:\s*(Low|Medium|High|Undefined)", re.IGNORECASE)
_PREFIX_RE = re.compile(r"^\s*(?:[\w./\\-]+):\d+(?::\d+)?:\s*([A-Z])\d{3,4}(?::|\s)", re.IGNORECASE)
_PYLINT_SUFFIX = re.compile(r"\([a-z0-9-]+\)\s*$", re.IGNORECASE)

_SYSTEM_PROMPT = (
    "You score the operational severity of a CI/CD build job that failed."
    " You receive the predicted ROOT CAUSE plus a block of evidence log lines."
    " Return ONLY a JSON object with two keys:\n"
    '  {"severity": "Low"|"Medium"|"High"|"Critical", "reason": "<one short sentence>"}\n'
    "Rubric:\n"
    '- Low: cosmetic/warning-level issue, build continues; e.g. lint style notes, deprecation, flaky retry.\n'
    '- Medium: partial failure or transient problem that blocks a step but not the whole system; '
    "e.g. test flake, timeout with retry, warning that needs attention.\n"
    '- High: a real failure requiring a fix; e.g. compile/test/build failure, dependency install error, '
    "deployment error, auth/permission failure.\n"
    '- Critical: outage, security breach, data loss, production impact, leaked secret, disk full, '
    "fatal/crash of the whole pipeline or service.\n"
    "When in doubt prefer the boundary that the log text itself shows."
)


def _tool_severity(line):
    m = _BANDIT_SEV.search(line)
    if m:
        s = m.group(1).capitalize()
        return 'Medium' if s == 'Undefined' else s
    if line.lstrip().startswith('>> Issue:'):
        return 'Low'
    m = _PREFIX_RE.search(line)
    if m:
        code = m.group(1).upper()
        table = _PYLINT_MAP if _PYLINT_SUFFIX.search(line) else _FLAKE8_MAP
        return table.get(code)
    return None


def policy_severity(root_cause, text):
    """Deterministic fallback: tool code > upgrade/downgrade signal > root-cause baseline.
    `text` is the flattened evidence block (or a single line)."""
    tool = _tool_severity(text)
    if tool:
        return tool
    lower = text.lower()
    baseline = _ROOT_CAUSE_BASELINES.get(root_cause, "Medium")
    if any(s in lower for s in _UPGRADE_SIGNALS):
        return "Critical"
    if any(s in lower for s in _DOWNGRADE_SIGNALS):
        lowered = "Medium" if _SEVERITY_RANK[baseline] > _SEVERITY_RANK["Medium"] else baseline
        return lowered
    return baseline


def _user_text(root_cause, evidence):
    return f"root cause: {root_cause}\n\nevidence log lines:\n{evidence}"


def _gemini_body(root_cause, evidence, force_json):
    cfg = {"temperature": 0, "maxOutputTokens": 1000}
    if force_json:
        cfg["responseMimeType"] = "application/json"
    if os.environ.get("GEMINI_THINKING", "0") != "1":
        cfg["thinkingConfig"] = {"thinkingBudget": 0}  # gemini 3.x is a thinking model by default
    return {
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": _user_text(root_cause, evidence)}]}],
        "generationConfig": cfg,
    }


def _gemini_once(url, api_key, root_cause, evidence, timeout, force_json):
    req = urllib.request.Request(
        url + "?key=" + api_key,
        data=json.dumps(_gemini_body(root_cause, evidence, force_json)).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _retry_delay(e, attempt):
    """Seconds to wait before retrying: honor Gemini's retryDelay when provided."""
    try:
        body = e.read().decode("utf-8")
        err = json.loads(body).get("error", {})
        delay = err.get("retryDelay")
        if not delay:
            for d in err.get("details", []):
                if d.get("@type", "").endswith("RetryInfo") and d.get("retryDelay"):
                    delay = d["retryDelay"]
                    break
        if not delay:
            m = re.search(r"[Rr]etry in ([\d.]+)s", err.get("message", ""))
            if m:
                delay = m.group(1) + "s"
        if delay:
            return min(float(str(delay).rstrip("s")), 120)
    except Exception:
        pass
    return min(2 ** attempt + random.uniform(0, 1), 60)


def _call_gemini(root_cause, evidence, api_key, timeout):
    primary = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    models = [primary] + [m for m in _MODEL_FALLBACKS if m != primary]
    last_err = None
    for model in models:
        url = f"{GEMINI_URL}/{model}:generateContent"
        data = None
        for attempt in range(MAX_RETRIES):
            try:
                data = _gemini_once(url, api_key, root_cause, evidence, timeout, True)
                break
            except urllib.error.HTTPError as e:
                if e.code == 400:  # model may not support JSON mode -> retry once without it
                    try:
                        data = _gemini_once(url, api_key, root_cause, evidence, timeout, False)
                        break
                    except urllib.error.HTTPError as e2:
                        e = e2
                last_err = e
                # 429 = per-model daily quota exhausted -> try next model immediately
                if e.code == 429:
                    break
                if e.code not in RETRYABLE_CODES or attempt == MAX_RETRIES - 1:
                    if e.code in RETRYABLE_CODES:
                        break  # next model
                    raise
                time.sleep(_retry_delay(e, attempt))
        if data is not None:
            candidates = (data or {}).get("candidates", []) or []
            if not candidates:
                raise RuntimeError("no candidates: " + json.dumps(data)[:200])
            return "".join(p.get("text", "") for p in candidates[0]["content"]["parts"])
    if last_err is not None:
        raise last_err
    raise RuntimeError("all Gemini models failed")


def _parse_severity(text):
    try:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            obj = json.loads(text[start:end + 1])
            sev = obj.get("severity")
            if isinstance(sev, str):
                sev = sev.capitalize()
            if sev in SEVERITY_LEVELS:
                return sev, obj.get("reason")
    except json.JSONDecodeError:
        pass
    m = re.search(r'"(severity)"\s*:\s*"([A-Za-z]+)"', text)
    if not m:
        return None, None
    sev = m.group(1).capitalize()
    if sev not in SEVERITY_LEVELS:
        return None, None
    rm = re.search(r'"(reason)"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    return sev, rm.group(2) if rm else None


def _try_llm(root_cause, evidence, timeout):
    """Gemini, raising on total failure. Returns (severity, reason, provider)."""
    provider = PROVIDER
    if provider == "none":
        raise RuntimeError("SEVERITY_LLM_PROVIDER=none (disabled)")
    if provider != "gemini":
        raise RuntimeError(f"unsupported SEVERITY_LLM_PROVIDER={provider!r}")
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    return *_parse_severity(_call_gemini(root_cause, evidence, key, timeout)), "gemini"


_CATEGORY_PROMPT = (
    "You classify a CI/CD build log into exactly one of the given candidate categories. "
    "Return ONLY a JSON object with two keys:\n"
    '  {"category": "<one of the candidates, or \\"unknown\\">", "reason": "<one short sentence>"}\n'
    "Pick \"unknown\" when the log does not clearly match any candidate."
)


def adjudicate_category(candidates, evidence, timeout=60):
    """Gemini adjudication for low-confidence classifier outputs.

    `candidates` is a non-empty list of category labels, `evidence` the log text.
    Returns (label, reason) where label is one of candidates or "unknown".
    Raises RuntimeError when the LLM is disabled/unavailable so callers can
    fall back to "unknown" without storing a misleading verdict.
    """
    candidates = [c for c in candidates if c and c != "unknown"]
    if not candidates:
        raise RuntimeError("no candidates to adjudicate")
    provider = PROVIDER
    if provider == "none":
        raise RuntimeError("SEVERITY_LLM_PROVIDER=none (disabled)")
    if provider != "gemini":
        raise RuntimeError(f"unsupported SEVERITY_LLM_PROVIDER={provider!r}")
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    prompt = (
        _CATEGORY_PROMPT
        + "\nCandidates: " + ", ".join(candidates)
        + "\n\nLog:\n" + (evidence or "")[:6000]
    )
    body = {
        "system_instruction": {"parts": [{"text": prompt}]},
        "contents": [{"parts": [{"text": "Classify this log."}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 300},
    }
    primary = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    models = [primary] + [m for m in _MODEL_FALLBACKS if m != primary]
    last_err = None
    for model in models:
        try:
            req = urllib.request.Request(
                f"{GEMINI_URL}/{model}:generateContent?key=" + key,
                data=json.dumps(body).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                obj = json.loads(text[start:end + 1])
                label = obj.get("category")
                if label in candidates:
                    return label, obj.get("reason") or ""
                if label == "unknown":
                    return "unknown", obj.get("reason") or ""
            last_err = RuntimeError("unparseable LLM output: " + text[:200])
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                continue  # quota exhausted -> next model
            time.sleep(_retry_delay(e, 0))
        except Exception as e:  # noqa: BLE001 - adjudication must never crash callers
            last_err = e
    raise RuntimeError(f"all Gemini models failed: {type(last_err).__name__}: {last_err}")
def classify_severity(root_cause, evidence, api_key=None, model=None, timeout=120, fallback_policy=True):
    """Gemini severity with deterministic policy as fallback.
    With fallback_policy=False, a failed/unparseable LLM call returns
    (None, None, reason) so callers can leave llm_severity NULL instead of
    storing a misleading rule-based verdict.
    Returns (severity, source, reason) or (None, None, reason)."""
    if api_key:
        os.environ["GEMINI_API_KEY"] = api_key
    if model:
        os.environ["GEMINI_MODEL"] = model
    try:
        sev, reason, provider = _try_llm(root_cause, evidence, timeout)
        if sev is not None:
            return sev, provider, reason or ""
        if fallback_policy:
            return policy_severity(root_cause, evidence), "policy", "unparseable LLM output"
        return None, None, "unparseable LLM output"
    except Exception as e:
        if fallback_policy:
            return policy_severity(root_cause, evidence), "policy", f"llm error: {type(e).__name__}: {e}"
        return None, None, f"llm error: {type(e).__name__}: {e}"