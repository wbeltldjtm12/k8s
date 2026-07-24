# KUBEIN CI/CD initial setup

The repository contains two GitHub Actions workflows:

- `deploy.yml`: builds backend/frontend images, pushes commit and `latest`
  tags to Docker Hub, and deploys the commit tag on the master node.
- `monitoring.yml`: installs or upgrades the pinned kube-prometheus-stack.

Complete the secret and runner setup before the first push so the initial
workflows do not fail or remain queued.

## 1. Create an empty GitHub repository

This directory is not currently a Git repository. Create an empty GitHub
repository, but do not push the source yet.

Use a private repository if possible because the self-hosted runner can deploy
to the Kubernetes master.

## 2. Add the Docker Hub secret

Create a Docker Hub access token with read/write access. In the empty GitHub
repository, open:

`Settings > Secrets and variables > Actions > New repository secret`

Create:

```text
DOCKERHUB_TOKEN=<Docker Hub access token>
```

The workflow uses the Docker Hub namespace `sch02`. Do not store the Docker Hub
password, Gemini key, or `.env.cluster` in Git.

## 3. Create the persistent server configuration

On the Kubernetes master:

```bash
sudo mkdir -p /home/master/kubein-config
sudo mkdir -p /home/master/kubein-data/chroma_db
sudo chown -R master:master /home/master/kubein-config /home/master/kubein-data
nano /home/master/kubein-config/.env.cluster
```

The file should contain:

```dotenv
KUBEINSIGHT_ENV=prod
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_MODEL=gemini-2.5-flash
LLM_API_KEY=replace-with-your-gemini-api-key
PROMETHEUS_URL=http://192.168.67.13:30090
```

Verify that the runner user can access Docker and Kubernetes:

```bash
docker ps
kubectl get nodes
test -r /home/master/.kube/config
```

## 4. Register the self-hosted runner

In the GitHub repository, open:

`Settings > Actions > Runners > New self-hosted runner`

Select Linux and x64, then run the download commands GitHub displays on the
master. During `config.sh`, add the custom label:

```bash
./config.sh --url https://github.com/OWNER/REPOSITORY \
  --token ONE_TIME_TOKEN \
  --labels kubein-master
```

Install it as a service so deployments continue after logout:

```bash
sudo ./svc.sh install
sudo ./svc.sh start
sudo ./svc.sh status
```

The runner should show `Idle` in GitHub before the first push.

## 5. Initialize Git and make the first push

From the KUBEIN root on the development PC:

```bash
git init -b main
git add .
git commit -m "Add KUBEIN CI/CD"
git remote add origin https://github.com/OWNER/REPOSITORY.git
git push -u origin main
```

The first push starts both workflows. They can also be run manually from the
GitHub Actions page.

Verify on the master:

```bash
docker ps
curl -fsS http://127.0.0.1:8000/api/health
curl -fsS http://127.0.0.1:8501/_stcore/health
curl -fsS http://192.168.67.13:30090/-/ready
```

Every later push to `main` that changes the backend, frontend, Compose file, or
deployment workflow builds commit-tagged images and deploys that exact commit.
