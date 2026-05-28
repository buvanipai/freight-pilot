#!/usr/bin/env bash
# ONE-SHOT INFRASTRUCTURE PROVISIONER — run once to create the EC2 instance,
# security groups, IAM profile, and key pair.
# Ongoing deploys are handled by .github/workflows/deploy-api.yml.
set -euo pipefail

AWS_REGION="us-east-2"
VPC_ID="vpc-0066ceabfbc521438"
SUBNET_ID="subnet-0db97ccd95714d360"
RDS_REDIS_SG="sg-09c17fa7c85567499"
ACCOUNT_ID="526247032705"
IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/freight-pilot:latest"
KEY_PATH="/Users/buvanipai/Documents/Projects/baton-freight-pilot/freight-pilot-ec2-key.pem"

ANTHROPIC_KEY=$(grep '^ANTHROPIC_API_KEY=' .env | cut -d= -f2-)

cat > /tmp/ec2-trust.json <<'EOF'
{
  "Version":"2012-10-17",
  "Statement":[{
    "Effect":"Allow",
    "Principal":{"Service":"ec2.amazonaws.com"},
    "Action":"sts:AssumeRole"
  }]
}
EOF

aws iam create-role --role-name FreightPilotEc2Role --assume-role-policy-document file:///tmp/ec2-trust.json >/dev/null 2>&1 || true
aws iam attach-role-policy --role-name FreightPilotEc2Role --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly >/dev/null
aws iam attach-role-policy --role-name FreightPilotEc2Role --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore >/dev/null
aws iam create-instance-profile --instance-profile-name FreightPilotEc2Profile >/dev/null 2>&1 || true
aws iam add-role-to-instance-profile --instance-profile-name FreightPilotEc2Profile --role-name FreightPilotEc2Role >/dev/null 2>&1 || true

if ! aws ec2 describe-key-pairs --region "$AWS_REGION" --key-names freight-pilot-ec2-key >/dev/null 2>&1; then
  aws ec2 create-key-pair --region "$AWS_REGION" --key-name freight-pilot-ec2-key --query 'KeyMaterial' --output text > "$KEY_PATH"
  chmod 400 "$KEY_PATH"
fi

APP_SG_ID=$(aws ec2 create-security-group --region "$AWS_REGION" --group-name freight-pilot-app-sg --description 'Freight Pilot EC2 app SG' --vpc-id "$VPC_ID" --query 'GroupId' --output text 2>/dev/null || aws ec2 describe-security-groups --region "$AWS_REGION" --filters Name=group-name,Values=freight-pilot-app-sg Name=vpc-id,Values="$VPC_ID" --query 'SecurityGroups[0].GroupId' --output text)

aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$APP_SG_ID" --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null 2>&1 || true
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$APP_SG_ID" --protocol tcp --port 22 --cidr 0.0.0.0/0 >/dev/null 2>&1 || true
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$RDS_REDIS_SG" --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":5432,\"ToPort\":5432,\"UserIdGroupPairs\":[{\"GroupId\":\"$APP_SG_ID\"}]},{\"IpProtocol\":\"tcp\",\"FromPort\":6379,\"ToPort\":6379,\"UserIdGroupPairs\":[{\"GroupId\":\"$APP_SG_ID\"}]}]" >/dev/null 2>&1 || true

AMI_ID=$(aws ec2 describe-images \
  --region "$AWS_REGION" \
  --owners amazon \
  --filters "Name=name,Values=al2023-ami-*-x86_64" "Name=state,Values=available" \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
  --output text)

cat > /tmp/freight-user-data.sh <<EOF
#!/bin/bash
set -euxo pipefail
AWS_REGION="${AWS_REGION}"
ACCOUNT_ID="${ACCOUNT_ID}"
IMAGE_URI="${IMAGE_URI}"
yum update -y
yum install -y docker
systemctl enable docker
systemctl start docker
aws ecr get-login-password --region "\$AWS_REGION" | docker login --username AWS --password-stdin "\${ACCOUNT_ID}.dkr.ecr.\${AWS_REGION}.amazonaws.com"
docker pull "\$IMAGE_URI"
docker rm -f freight-pilot-api || true
docker run -d --name freight-pilot-api --restart unless-stopped -p 80:8080 \
  -e DATABASE_URL='postgresql://freightpilot:wybTon-nakpeg-3gujfy@freightpilot-db.cvqc6a80m97n.us-east-2.rds.amazonaws.com:5432/freightpilot' \
  -e REDIS_URL='redis://clustercfg.freightpilot.j48pai.use2.cache.amazonaws.com:6379' \
  -e REDIS_CLUSTER='true' \
  -e ANTHROPIC_API_KEY='${ANTHROPIC_KEY}' \
  -e CORS_ORIGINS='https://buvanipai.github.io,https://buvanipai.github.io/freight-pilot' \
  -e PORT='8080' \
  "\$IMAGE_URI"
EOF

INSTANCE_ID=$(aws ec2 run-instances --region "$AWS_REGION" \
  --image-id "$AMI_ID" \
  --instance-type t3.micro \
  --iam-instance-profile Name=FreightPilotEc2Profile \
  --key-name freight-pilot-ec2-key \
  --subnet-id "$SUBNET_ID" \
  --security-group-ids "$APP_SG_ID" \
  --associate-public-ip-address \
  --user-data file:///tmp/freight-user-data.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=freight-pilot-ec2}]' \
  --query 'Instances[0].InstanceId' --output text)

aws ec2 wait instance-running --region "$AWS_REGION" --instance-ids "$INSTANCE_ID"
PUBLIC_IP=$(aws ec2 describe-instances --region "$AWS_REGION" --instance-ids "$INSTANCE_ID" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

echo "INSTANCE_ID=$INSTANCE_ID"
echo "PUBLIC_IP=$PUBLIC_IP"
echo "SSH: ssh -i $KEY_PATH ec2-user@$PUBLIC_IP"
echo "URL: http://$PUBLIC_IP/health"
