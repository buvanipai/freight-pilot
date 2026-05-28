# Deploying Freight Pilot on AWS App Runner

This guide walks through every step from zero to a live URL, including exactly where to get each credential.

This version is optimized for a full AWS setup and a UI-first flow (AWS Console first, minimal CLI).

---

## Part 0 - Choose Your Deployment Track

If you want reliability and one cloud provider, use this path:

1. RDS for PostgreSQL
2. ElastiCache for Redis (Redis Streams supported)
3. ECR for container images
4. App Runner for hosting

This is the recommended track in this document.

---

## Part 1 — Get Your Credentials

### 1.1 AWS Access Keys

You need these to run `aws` CLI commands from your machine.

> **Do not create a root user access key.** AWS will warn you against this — root keys have unlimited permissions and cannot be scoped down. Use an IAM user instead (steps below).

#### Create an IAM user with the right permissions

1. Go to [https://console.aws.amazon.com/iam](https://console.aws.amazon.com/iam) → **Users** → **Create user**.
2. Username: `freight-pilot-deployer` → **Next**.
3. Select **Attach policies directly** → search and attach these three policies:
   - `AmazonEC2ContainerRegistryFullAccess`
   - `AWSAppRunnerFullAccess`
   - `IAMFullAccess` *(needed to create the App Runner ECR role in Part 4)*
4. **Next** → **Create user**.
5. Click the new `freight-pilot-deployer` user → **Security credentials** tab → **Create access key**.
6. Choose **CLI** as the use case → **Next**.
7. AWS will show a screen recommending alternatives like `aws login` (IAM Identity Center) or CloudShell. These are for enterprise/team setups. Since you're deploying from a local machine and need to run `docker build` locally, neither alternative applies here. Check **"I understand the above recommendation and want to proceed"** → **Create access key**.
8. Copy both values — you will **not** be able to see the secret again:
   - **Access key ID** → looks like `AKIAIOSFODNN7EXAMPLE`
   - **Secret access key** → looks like `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`

Now configure the CLI:

```bash
aws configure
# AWS Access Key ID:     <paste Access key ID>
# AWS Secret Access Key: <paste Secret access key>
# Default region name:   us-east-2
# Default output format: json
```

Verify it works:

```bash
aws sts get-caller-identity
```

You should see your account ID and user ARN printed back.

---

### 1.2 DATABASE_URL (PostgreSQL)

#### Option B — Amazon RDS (fully AWS-hosted, recommended)

1. Go to [https://console.aws.amazon.com/rds](https://console.aws.amazon.com/rds) → **Create database**.
2. If you only see a simple screen with "Set up EC2 connection" and "Easy create", switch to **Standard create** first.
3. In **Compute resource**, choose **Don't connect to an EC2 compute resource**.
4. Choose **PostgreSQL**, version **15**, tier **Free tier** (for testing) or **db.t3.micro**.
5. Set:
   - **DB instance identifier**: `freightpilot-db`
   - **Master username**: `freightpilot`
   - **Master password**: choose a strong password
6. Under **Connectivity**:
  - for fastest first deploy: **Public access = Yes**
  - for production hardening: **Public access = No** and use App Runner VPC Connector (see UI steps in Part 2)
7. After creation (~5 min), go to the database → **Connectivity & security** → copy the **Endpoint**.
8. Your `DATABASE_URL` will be:  
   `postgresql://freightpilot:PASSWORD@ENDPOINT:5432/freightpilot`

---

### 1.3 REDIS_URL

#### Option C — Amazon ElastiCache (fully AWS-hosted, recommended)

1. Go to [https://console.aws.amazon.com/elasticache](https://console.aws.amazon.com/elasticache) → **Create cluster** → **Redis OSS**.
2. Use **Serverless** mode (simplest) or a `cache.t3.micro` node.
3. The endpoint will be:  
  `redis://your-cluster.XXXX.ng.0001.use2.cache.amazonaws.com:6379`
4. ElastiCache supports Redis Streams, so your producer-consumer pipeline works unchanged.
5. ElastiCache clusters are VPC-internal by default — you'll need App Runner VPC Connector steps in Part 2.

---

### 1.4 ANTHROPIC_API_KEY

1. Go to [https://console.anthropic.com](https://console.anthropic.com) → **API Keys**.
2. Click **Create Key** → name it `freight-pilot` → copy the key.
3. It looks like: `sk-ant-api03-XXXXXXXXXXXX`

---

## Part 2 — UI-First Full AWS Deployment (Recommended)

This section is the primary path if you prefer AWS Console over CLI.

### 2.1 Create ECR Repository (UI)

1. Go to [https://console.aws.amazon.com/ecr](https://console.aws.amazon.com/ecr).
2. Open **Repositories** → **Create repository**.
3. Name: `freight-pilot`.
4. Turn on **Scan on push**.
5. Click **Create repository**.
6. Copy the repository URI shown on the repo page.

### 2.2 Push Your Local Docker Image to ECR (Minimal CLI)

Use these in your local terminal from this project folder:

```bash
export AWS_REGION="us-east-2"
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REPO="freight-pilot"
export IMAGE_TAG="latest"
export ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin \
  "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

docker build --platform linux/amd64 -t "${ECR_REPO}:${IMAGE_TAG}" .
docker tag "${ECR_REPO}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
docker push "${ECR_URI}:${IMAGE_TAG}"
```

### 2.3 Create App Runner ECR Access Role (UI)

1. Go to [https://console.aws.amazon.com/iam](https://console.aws.amazon.com/iam) → **Roles** → **Create role**.
2. Trusted entity type: **AWS service**.
3. Use case: **App Runner**.
4. Attach policy: `AWSAppRunnerServicePolicyForECRAccess`.
5. Role name: `AppRunnerECRAccessRole`.
6. Create role.

### 2.4 Create VPC Connector (UI, required for private RDS/ElastiCache)

1. Go to [https://console.aws.amazon.com/apprunner](https://console.aws.amazon.com/apprunner).
2. Open **VPC connectors** → **Create**.
3. Name: `freight-pilot-vpc`.
4. Select the same VPC/subnets where RDS and ElastiCache live (at least 2 subnets).
5. Choose a security group that allows outbound to:
  - PostgreSQL port 5432
  - Redis port 6379
6. Create connector.

### 2.5 Create App Runner Service (UI)

1. In App Runner, click **Create service**.
2. Source: **Container registry**.
3. Provider: **Amazon ECR**.
4. Select repository `freight-pilot`, image tag `latest`.
5. Deployment trigger: **Automatic**.
6. Service settings:
  - Service name: `freight-pilot-api`
  - Port: `8080`
  - CPU/Memory: start with `1 vCPU / 2 GB`
7. Environment variables:
  - `DATABASE_URL` = your RDS URL
  - `REDIS_URL` = your ElastiCache URL
  - `ANTHROPIC_API_KEY` = your Anthropic key
  - `CORS_ORIGINS` = `https://buvanipai.github.io,https://buvanipai.github.io/freight-pilot`
  - `PORT` = `8080`
8. Networking:
  - Egress type: **Custom VPC**
  - VPC connector: `freight-pilot-vpc`
9. Health check:
  - Protocol: HTTP
  - Path: `/health`
10. Create service.

### 2.6 Get the Live URL (UI)

1. Open the new App Runner service.
2. Wait until status is **Running**.
3. Copy **Default domain**. That is your live URL.

### 2.7 Redeploy After Changes (UI)

1. Build and push a new image to the same ECR tag.
2. App Runner auto-deploys if automatic deployment is enabled.
3. You can also choose **Deploy** from the App Runner service page.

---

## Part 3 — CLI Reference (Optional)

Open a terminal in the project folder and set these once. Replace the placeholder values.

```bash
export AWS_REGION="us-east-2"
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REPO="freight-pilot"
export IMAGE_TAG="latest"
export ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

# Your credentials from Part 1
export DATABASE_URL="postgresql://freightpilot:PASSWORD@HOST:5432/freightpilot"
export REDIS_URL="redis://default:PASSWORD@HOST:6379"
export ANTHROPIC_API_KEY="sk-ant-api03-XXXXXXXXXXXX"
```

---

## Part 4 — Build and Push the Docker Image to ECR

### 3.1 Create the ECR repository

```bash
aws ecr create-repository \
  --repository-name "${ECR_REPO}" \
  --region "${AWS_REGION}" \
  --image-scanning-configuration scanOnPush=true \
  --encryption-configuration encryptionType=AES256
```

You'll see JSON output. The `repositoryUri` field confirms your image path.

### 3.2 Authenticate Docker with ECR

```bash
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin \
    "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
```

You should see `Login Succeeded`.

### 3.3 Build the image

> **Apple Silicon (M1/M2/M3) users:** the `--platform linux/amd64` flag is required — App Runner only runs x86-64.

```bash
docker build --platform linux/amd64 -t "${ECR_REPO}:${IMAGE_TAG}" .
```

### 3.4 Tag and push

```bash
docker tag "${ECR_REPO}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
docker push "${ECR_URI}:${IMAGE_TAG}"
```

### 3.5 Confirm the image is in ECR

```bash
aws ecr describe-images \
  --repository-name "${ECR_REPO}" \
  --region "${AWS_REGION}" \
  --query 'imageDetails[*].{Tag:imageTags[0],Pushed:imagePushedAt}' \
  --output table
```

---

## Part 5 — Create the IAM Role for App Runner

App Runner needs permission to pull images from ECR.

```bash
# Write the trust policy
cat > /tmp/apprunner-trust.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "build.apprunner.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
EOF

# Create the role
aws iam create-role \
  --role-name AppRunnerECRAccessRole \
  --assume-role-policy-document file:///tmp/apprunner-trust.json

# Attach the AWS-managed ECR read policy
aws iam attach-role-policy \
  --role-name AppRunnerECRAccessRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess

# Store the role ARN for use in the next step
export ECR_ROLE_ARN=$(aws iam get-role \
  --role-name AppRunnerECRAccessRole \
  --query 'Role.Arn' --output text)

echo "Role ARN: ${ECR_ROLE_ARN}"
```

---

## Part 6 — Create the App Runner Service

```bash
cat > /tmp/apprunner-service.json <<EOF
{
  "ServiceName": "freight-pilot-api",
  "SourceConfiguration": {
    "AuthenticationConfiguration": {
      "AccessRoleArn": "${ECR_ROLE_ARN}"
    },
    "AutoDeploymentsEnabled": true,
    "ImageRepository": {
      "ImageIdentifier": "${ECR_URI}:${IMAGE_TAG}",
      "ImageConfiguration": {
        "Port": "8080",
        "RuntimeEnvironmentVariables": {
          "DATABASE_URL":      "${DATABASE_URL}",
          "REDIS_URL":         "${REDIS_URL}",
          "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}",
          "CORS_ORIGINS":      "https://buvanipai.github.io,https://buvanipai.github.io/freight-pilot",
          "PORT":              "8080"
        }
      },
      "ImageRepositoryType": "ECR"
    }
  },
  "InstanceConfiguration": {
    "Cpu":    "1 vCPU",
    "Memory": "2 GB"
  },
  "HealthCheckConfiguration": {
    "Protocol":           "HTTP",
    "Path":               "/health",
    "Interval":           10,
    "Timeout":            5,
    "HealthyThreshold":   1,
    "UnhealthyThreshold": 5
  }
}
EOF

aws apprunner create-service \
  --cli-input-json file:///tmp/apprunner-service.json \
  --region "${AWS_REGION}"
```

Copy the `ServiceArn` from the output — you'll use it below.

---

## Part 7 (Optional) — VPC Connector for ElastiCache or RDS in a Private VPC

Skip this if you're using Upstash Redis or Render Postgres (public endpoints).

If your database or Redis is inside an AWS VPC (e.g. ElastiCache, private RDS):

1. Go to [https://console.aws.amazon.com/vpc](https://console.aws.amazon.com/vpc) → **Subnets**.  
   Copy the IDs of 2+ subnets in the same VPC as your RDS/ElastiCache.

2. Go to **Security Groups** and find (or create) one that allows outbound traffic to your DB port (5432) and Redis port (6379).

3. Create the connector:

```bash
aws apprunner create-vpc-connector \
  --vpc-connector-name freight-pilot-vpc \
  --subnets subnet-XXXXXX subnet-YYYYYY \
  --security-groups sg-ZZZZZZ \
  --region "${AWS_REGION}"
```

4. Copy the `VpcConnectorArn` and update your service:

```bash
aws apprunner update-service \
  --service-arn "<your-service-arn>" \
  --network-configuration '{
    "EgressConfiguration": {
      "EgressType": "VPC",
      "VpcConnectorArn": "<vpc-connector-arn>"
    }
  }' \
  --region "${AWS_REGION}"
```

---

## Part 8 — Check Deployment Status and Get the Live URL

The first deploy takes about 3–5 minutes.

```bash
# Store your service ARN
export SERVICE_ARN=$(aws apprunner list-services \
  --region "${AWS_REGION}" \
  --query "ServiceSummaryList[?ServiceName=='freight-pilot-api'].ServiceArn" \
  --output text)

# Poll status
aws apprunner describe-service \
  --service-arn "${SERVICE_ARN}" \
  --region "${AWS_REGION}" \
  --query 'Service.{Status:Status, URL:ServiceUrl}' \
  --output table
```

When `Status` shows `RUNNING`, your app is live at:

```
https://<random-id>.us-east-2.awsapprunner.com
```

Test it:

```bash
curl https://<your-url>.awsapprunner.com/health
```

---

## Part 9 — Redeploying After Code Changes

Because `AutoDeploymentsEnabled` is `true`, App Runner polls ECR for image digest changes. Push a new image to trigger a redeploy:

```bash
docker build --platform linux/amd64 -t "${ECR_REPO}:${IMAGE_TAG}" .
docker tag "${ECR_REPO}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
docker push "${ECR_URI}:${IMAGE_TAG}"
```

App Runner detects the new digest and rolls out the update automatically (no downtime).

To trigger a manual redeploy without a code change:

```bash
aws apprunner start-deployment \
  --service-arn "${SERVICE_ARN}" \
  --region "${AWS_REGION}"
```

---

## Credentials Summary

| Variable | Where to get it |
|---|---|
| AWS Access Key ID | IAM → Users → freight-pilot-deployer → Security credentials → Create access key |
| AWS Secret Access Key | Same page, shown once at creation |
| `DATABASE_URL` | AWS RDS Console → Databases → freightpilot-db → Endpoint |
| `REDIS_URL` | AWS ElastiCache Console → Redis cluster/serverless cache → Primary endpoint |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |

---

## Estimated AWS Costs

| Resource | Free tier | Paid rate |
|---|---|---|
| App Runner | 1M requests/mo free | ~$0.064/vCPU-hr + $0.007/GB-hr |
| ECR | 500 MB/mo free | $0.10/GB-mo after |
| RDS (if used) | 750 hr/mo free (t3.micro) | ~$0.017/hr after |
| ElastiCache (if used) | Not in free tier | ~$0.017/hr (t3.micro) |

For a pilot / low-traffic app, this full AWS setup is operationally cleaner, but ElastiCache usually becomes the first paid component.
