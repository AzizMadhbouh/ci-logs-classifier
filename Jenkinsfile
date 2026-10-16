pipeline {
    agent any

    environment {
        DB_HOST = 'postgres-buildhistory'
        DB_PORT = '5432'
        DB_NAME = 'buildhistory'
        DB_USER = 'builduser'
        DB_PASSWORD = credentials('db-buildpass')
    }

    stages {
        stage('Setup') {
            steps {
                sh 'apt-get update && apt-get install -y python3 python3-pip python3-venv'
                sh 'python3 -m venv venv'
                sh '. venv/bin/activate && pip install -r requirements.txt'
                sh '. venv/bin/activate && pip install flake8 black mypy bandit scikit-learn pylint flake8-sarif-formatter'
            }
        }

        stage('Analyze') {
            steps {
                sh '. venv/bin/activate && black --check feed_jenkins_builds.py watch_builds.py db.py ml/ tests/ > build-output.log 2>&1 || true'
                sh '. venv/bin/activate && flake8 --format=sarif --output-file=flake8.sarif feed_jenkins_builds.py watch_builds.py db.py ml/ tests/ || true'
                sh '. venv/bin/activate && flake8 feed_jenkins_builds.py watch_builds.py db.py ml/ tests/ >> build-output.log 2>&1 || true'
                sh '. venv/bin/activate && mypy feed_jenkins_builds.py db.py ml/ >> build-output.log 2>&1 || true'
                sh '. venv/bin/activate && bandit -r feed_jenkins_builds.py watch_builds.py db.py ml/ -f sarif -o bandit.sarif || true'
                sh '. venv/bin/activate && bandit -r feed_jenkins_builds.py watch_builds.py db.py ml/ >> build-output.log 2>&1 || true'
                sh '. venv/bin/activate && pylint --output-format=json:pylint.json feed_jenkins_builds.py watch_builds.py db.py ml/ >> build-output.log 2>&1 || true'
            }
        }

        stage('Test') {
            steps {
                sh '. venv/bin/activate && pytest --cov=feed_jenkins_builds --cov=ml --cov-report=html --junitxml=report.xml >> build-output.log 2>&1 || true'
            }
        }

        stage('Check') {
            steps {
                script {
                    def log = readFile 'build-output.log'
                    if (log =~ /Traceback \(most recent call last\)|\] FAILED|Fatal error|bandit - (?:results|issues) found/) {
                        error("Build failed: errors found in static analysis / tests")
                    }
                }
            }
        }
    }

    post {
        always {
            script {
                // Ingestion into Postgres is handled by feed_jenkins_builds.py / watch_builds.py,
                // not by the legacy analyze_history.py --save path.
                def buildResult = currentBuild.result ?: 'SUCCESS'
                sh "echo ${buildResult} > build-result.txt || true"
            }
            archiveArtifacts artifacts: '*.sarif, *.json, build-output.log', allowEmptyArchive: true
            archiveArtifacts artifacts: 'htmlcov/**', allowEmptyArchive: true
            archiveArtifacts artifacts: 'report.xml', allowEmptyArchive: true
            archiveArtifacts artifacts: 'build-result.txt', allowEmptyArchive: true
        }
    }
}
