pipeline {
    agent any

    stages {

        stage('Clone Repository') {
            steps {
                git 'https://github.com/foxyiz/Code.git'
            }
        }

        stage('Install Dependencies') {
            steps {
                bat 'pip install -r requirements.txt'
            }
        }

        stage('Run FoXYiZ Framework') {
            steps {
                bat 'python fEngine.py'
            }
        }

    }
}
