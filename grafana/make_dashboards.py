#!/usr/bin/env python3
"""Generate Grafana dashboard JSON for CI build severity from PostgreSQL."""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards"
DS = {"type": "postgres", "uid": "buildhistory"}


def statistic(title, sql, unit="", calc="lastNotNull"):
    return {
        "id": None,
        "type": "stat",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": 4, "w": 4, "x": 0, "y": 0},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                "mappings": [],
            },
            "overrides": [],
        },
        "options": {"colorMode": "value", "graphMode": "area", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": [calc], "fields": "", "values": False}, "textMode": "auto"},
        "targets": [{"datasource": DS, "format": "table", "group": [], "hide": False, "rawSql": sql, "refId": "A"}],
        "interval": "",
    }


def timeseries(title, targets, gridPos, overrides=None):
    return {
        "id": None,
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {"custom": {"drawStyle": "line", "fillOpacity": 15, "lineWidth": 2, "spanNulls": True, "showPoints": "never"}, "mappings": [], "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}},
            "overrides": overrides or [],
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "multi", "sort": "desc"}},
        "targets": targets,
    }


def barchar(title, sql, gridPos):
    return {
        "id": None,
        "type": "barchart",
        "title": title,
        "datasource": DS,
        "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {"color": {"mode": "palette-classic"}, "custom": {"axisPlacement": "auto", "fillOpacity": 85, "barAlignment": 0, "lineWidth": 1, "showPoints": "never", "spanNulls": False, "stacking": {"group": "A", "mode": "none"}}, "mappings": [], "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]}},
            "overrides": [],
        },
        "options": {"colorByField": "n", "orientation": "auto", "xField": "severity", "y": {"min": 0}},
        "targets": [{"datasource": DS, "format": "table", "group": [], "hide": False, "rawSql": sql, "refId": "A"}],
    }


def piechart(title, sql, gridPos):
    return {
        "id": None,
        "type": "piechart",
        "title": title,
        "datasource": DS,
        "gridPos": gridPos,
        "fieldConfig": {"defaults": {"color": {"mode": "palette-classic"}, "mappings": [], "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]}}, "overrides": []},
        "options": {"displayLabels": ["name", "value"], "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "pieType": "pie", "reduceOptions": {"calcs": [""], "fields": "", "values": True}, "tooltip": {"mode": "single", "sort": "none"}},
        "targets": [{"datasource": DS, "format": "table", "group": [], "hide": False, "rawSql": sql, "refId": "A"}],
    }


def table(title, sql, gridPos, overrides=None):
    return {
        "id": None,
        "type": "table",
        "title": title,
        "datasource": DS,
        "gridPos": gridPos,
        "fieldConfig": {"defaults": {"color": {"mode": "thresholds"}, "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False}, "mappings": [], "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]}}, "overrides": overrides or []},
        "options": {"cellHeight": "sm", "footer": {"countRows": False, "enablePagination": True, "show": False}, "showHeader": True, "sortBy": []},
        "targets": [{"datasource": DS, "format": "table", "group": [], "hide": False, "rawSql": sql, "refId": "A"}],
    }


SEV_MAPPINGS = {
    "type": "value",
    "options": {
        "Critical": {"text": "Critical", "color": "red", "index": 0},
        "High": {"text": "High", "color": "orange", "index": 1},
        "Medium": {"text": "Medium", "color": "yellow", "index": 2},
        "Low": {"text": "Low", "color": "green", "index": 3},
    },
}


def seq(panels):
    for i, p in enumerate(panels, start=1):
        p["id"] = i
    return panels


CATEGORY_SQL = "SELECT COALESCE(category, 'unknown') AS category, count(*)::int AS n FROM builds GROUP BY category ORDER BY n DESC"
CATEGORY_TREND_SQL = "SELECT $__time(b.timestamp), COALESCE(b.category, 'unknown') AS category, count(*)::int AS n FROM builds b WHERE $__timeFilter(b.timestamp) AND b.category IS NOT NULL GROUP BY 1, 2 ORDER BY 1"

def main():
    OUT.mkdir(parents=True, exist_ok=True)

    health = {
        "annotations": {"list": []},
        "editable": True,
        "graphTooltip": 1,
        "id": None,
        "links": [],
        "refresh": "30s",
        "schemaVersion": 39,
        "tags": ["ci", "severity"],
        "templating": {"list": []},
        "time": {"from": "now-30d", "to": "now"},
        "timezone": "browser",
        "title": "CI Build Severity & Health",
        "uid": "ci-build-severity",
        "version": 0,
        "panels": seq([
            statistic("Total Builds", "SELECT count(*) FROM builds WHERE $__timeFilter(timestamp)"),
            statistic("Failure Rate %", "SELECT COALESCE(count(*) FILTER (WHERE result = 'FAILURE') * 100.0 / NULLIF(count(*), 0), 0) AS rate FROM builds WHERE $__timeFilter(timestamp)", unit="percent"),
            statistic("Critical/High Issues", "SELECT count(*) FROM build_issues i JOIN builds b ON b.build_id = i.build_id WHERE $__timeFilter(b.timestamp) AND i.severity IN ('Critical', 'High')"),
            statistic("Latest Build LLM Severity", "SELECT llm_severity FROM builds ORDER BY timestamp DESC LIMIT 1"),
            timeseries("Issue Severity Trend", [
                {"datasource": DS, "format": "time_series", "rawSql": "SELECT $__time(b.timestamp), i.severity, count(*)::int AS n FROM build_issues i JOIN builds b ON b.build_id = i.build_id WHERE $__timeFilter(b.timestamp) AND i.severity IS NOT NULL GROUP BY 1, 2 ORDER BY 1", "refId": "A", "hide": False},
            ], {"h": 8, "w": 12, "x": 0, "y": 0}, overrides=[{"matcher": {"id": "byName", "options": "Critical"}, "properties": [{"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}}]}, {"matcher": {"id": "byName", "options": "High"}, "properties": [{"id": "color", "value": {"fixedColor": "orange", "mode": "fixed"}}]}, {"matcher": {"id": "byName", "options": "Medium"}, "properties": [{"id": "color", "value": {"fixedColor": "yellow", "mode": "fixed"}}]}, {"matcher": {"id": "byName", "options": "Low"}, "properties": [{"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}}]}]),
            timeseries("Errors & Warnings per Build", [
                {"datasource": DS, "format": "time_series", "rawSql": "SELECT $__time(timestamp), error_count AS \"errors\" FROM builds WHERE $__timeFilter(timestamp)", "refId": "A", "hide": False},
                {"datasource": DS, "format": "time_series", "rawSql": "SELECT $__time(timestamp), warning_count AS \"warnings\" FROM builds WHERE $__timeFilter(timestamp)", "refId": "B", "hide": False},
            ], {"h": 8, "w": 12, "x": 12, "y": 0}),
            barchar("Issues by Severity", "SELECT severity, count(*)::int AS n FROM build_issues GROUP BY severity ORDER BY n DESC", {"h": 9, "w": 12, "x": 0, "y": 0}),
            piechart("Severity Distribution", "SELECT severity, count(*)::int AS n FROM build_issues GROUP BY severity ORDER BY n DESC", {"h": 9, "w": 12, "x": 12, "y": 0}),
            table("Latest Builds", "SELECT b.build_id, b.timestamp, COALESCE(b.result, '?') AS result, b.error_count, b.warning_count, COALESCE(w.worst, '-') AS worst_severity, COALESCE(b.llm_severity, '-') AS llm_severity, COALESCE(b.category, '-') AS category, COALESCE(b.severity_reason, '-') AS reason FROM builds b LEFT JOIN LATERAL (SELECT i.severity AS worst FROM build_issues i WHERE i.build_id = b.build_id ORDER BY CASE i.severity WHEN 'Critical' THEN 4 WHEN 'High' THEN 3 WHEN 'Medium' THEN 2 WHEN 'Low' THEN 1 ELSE 0 END DESC LIMIT 1) w ON true ORDER BY b.timestamp DESC LIMIT 100", {"h": 10, "w": 24, "x": 0, "y": 0}, overrides=[
                {"matcher": {"id": "byName", "options": "worst_severity"}, "properties": [{"id": "mappings", "value": [SEV_MAPPINGS]}]},
                {"matcher": {"id": "byName", "options": "llm_severity"}, "properties": [{"id": "mappings", "value": [SEV_MAPPINGS]}]},
                {"matcher": {"id": "byName", "options": "result"}, "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text", "mode": "background"}}, {"id": "mappings", "value": [{"type": "value", "options": {"SUCCESS": {"color": "green", "index": 0}, "FAILURE": {"color": "red", "index": 1}}}]}]},
                {"matcher": {"id": "byName", "options": "reason"}, "properties": [{"id": "custom.width", "value": 420}]},
            ]),
        ]),
    }

    issues = {
        "annotations": {"list": []},
        "editable": True,
        "graphTooltip": 0,
        "id": None,
        "links": [],
        "refresh": "30s",
        "schemaVersion": 39,
        "tags": ["ci"],
        "templating": {"list": []},
        "time": {"from": "now-30d", "to": "now"},
        "timezone": "browser",
        "title": "CI Build Issues Deep Dive",
        "uid": "ci-build-issues",
        "version": 0,
        "panels": seq([
            piechart("Issue Severity", "SELECT severity, count(*)::int AS n FROM build_issues WHERE $__timeFilter(timestamp) GROUP BY severity ORDER BY n DESC", {"h": 9, "w": 8, "x": 0, "y": 0}),
            piechart("Issue Category", "SELECT COALESCE(category, 'other') AS category, count(*)::int AS n FROM build_issues GROUP BY category ORDER BY n DESC", {"h": 9, "w": 8, "x": 8, "y": 0}),
            barchar("Errors vs Warnings", "SELECT CASE WHEN is_error THEN 'error' ELSE 'warning' END AS kind, count(*)::int AS n FROM build_issues GROUP BY kind", {"h": 9, "w": 8, "x": 16, "y": 0}),
            table("Issue Detail", "SELECT build_id, timestamp, severity, COALESCE(category, 'other') AS category, line, is_error FROM build_issues WHERE $__timeFilter(timestamp) ORDER BY timestamp DESC LIMIT 100", {"h": 12, "w": 24, "x": 0, "y": 0}, overrides=[
                {"matcher": {"id": "byName", "options": "is_error"}, "properties": [{"id": "mappings", "value": [{"type": "value", "options": {"true": {"text": "error", "color": "red", "index": 0}, "false": {"text": "warning", "color": "yellow", "index": 1}}}]}]},
                {"matcher": {"id": "byName", "options": "severity"}, "properties": [{"id": "mappings", "value": [SEV_MAPPINGS]}]},
            ]),
            piechart("Category Distribution", CATEGORY_SQL, {"h": 9, "w": 8, "x": 0, "y": 13}),
            timeseries("Category Trend", [
                {"datasource": DS, "format": "time_series", "rawSql": CATEGORY_TREND_SQL, "refId": "A", "hide": False},
            ], {"h": 8, "w": 16, "x": 8, "y": 13}),
            table("Latest Builds with Category", "SELECT b.build_id, b.timestamp, COALESCE(b.result, '?') AS result, COALESCE(b.category, '-') AS category, COALESCE(b.llm_severity, '-') AS llm_severity, COALESCE(b.severity_reason, '-') AS reason, b.error_count, b.warning_count FROM builds b ORDER BY b.timestamp DESC LIMIT 50", {"h": 10, "w": 24, "x": 0, "y": 21}, overrides=[
                {"matcher": {"id": "byName", "options": "llm_severity"}, "properties": [{"id": "mappings", "value": [SEV_MAPPINGS]}]},
                {"matcher": {"id": "byName", "options": "result"}, "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text", "mode": "background"}}, {"id": "mappings", "value": [{"type": "value", "options": {"SUCCESS": {"color": "green", "index": 0}, "FAILURE": {"color": "red", "index": 1}}}]}]},
                {"matcher": {"id": "byName", "options": "reason"}, "properties": [{"id": "custom.width", "value": 420}]},
            ]),
        ]),
    }

    for name, dash in (("ci_build_severity.json", health), ("ci_build_issues.json", issues)):
        (OUT / name).write_text(json.dumps(dash, indent=2), encoding="utf-8")
        json.loads((OUT / name).read_text(encoding="utf-8"))
        print(f"wrote {OUT / name}")
    for stale in OUT.glob("ci_build_health.json"):
        stale.unlink()
        print(f"removed stale {stale}")
    print("OK")


if __name__ == "__main__":
    main()