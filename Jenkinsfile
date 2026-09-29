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
                sh '. venv/bin/activate && black --check src/ tests/ > build-output.log 2>&1 || true'
                sh '. venv/bin/activate && flake8 --format=sarif --output-file=flake8.sarif src/ tests/ || true'
                sh '. venv/bin/activate && flake8 src/ tests/ >> build-output.log 2>&1 || true'
                sh '. venv/bin/activate && mypy src/ >> build-output.log 2>&1 || true'
                sh '. venv/bin/activate && bandit -r src/ -f sarif -o bandit.sarif || true'
                sh '. venv/bin/activate && bandit -r src/ >> build-output.log 2>&1 || true'
                sh '. venv/bin/activate && pylint --output-format=json:pylint.json src/ >> build-output.log 2>&1 || true'
            }
        }

        stage('Test') {
            steps {
                sh '. venv/bin/activate && pytest --cov=src --cov-report=html --junitxml=report.xml >> build-output.log 2>&1 || true'
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
                def buildResult = currentBuild.result ?: 'SUCCESS'
                sh ". venv/bin/activate && python analyze_history.py --save \$BUILD_NUMBER analysis-report.txt --result ${buildResult} || true"
            }
            archiveArtifacts artifacts: '*.sarif, *.json, build-output.log', allowEmptyArchive: true
            archiveArtifacts artifacts: 'htmlcov/**', allowEmptyArchive: true
            archiveArtifacts artifacts: 'report.xml', allowEmptyArchive: true
            archiveArtifacts artifacts: 'analysis-report.txt', allowEmptyArchive: true
        }
    }
}
