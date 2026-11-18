# Deployment Guide

Complete guide for deploying Crypto Alert API to production.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Local Development](#local-development)
3. [Docker Deployment](#docker-deployment)
4. [Cloud Deployment](#cloud-deployment)
5. [Monitoring Setup](#monitoring-setup)
6. [Security Checklist](#security-checklist)
7. [Scaling Strategy](#scaling-strategy)

## Prerequisites

### System Requirements

- **CPU**: 2+ cores recommended
- **RAM**: 4GB minimum, 8GB recommended
- **Storage**: 20GB minimum
- **OS**: Linux (Ubuntu 22.04 LTS recommended)

### Software Dependencies

- Docker 24.0+
- Docker Compose 2.20+
- PostgreSQL 16+
- Redis 7+
- Python 3.11+ (for development)

## Local Development

### 1. Clone and Setup

```bash
git clone https://github.com/yourusername/crypto-alert-api.git
cd crypto-alert-api

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
ENVIRONMENT=development
DEBUG=true
LOG_LEVEL=DEBUG

DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/cryptoalert
REDIS_URL=redis://localhost:6379/0

# Generate secure secret key:
# python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=your-generated-secret-key-here
```

### 3. Start Services

```bash
# Start PostgreSQL and Redis
docker-compose up -d postgres redis

# Run migrations
alembic upgrade head

# Start API server
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

### 4. Verify Installation

```bash
# Health check
curl http://localhost:8000/api/v1/health

# API docs
open http://localhost:8000/docs
```

## Docker Deployment

### 1. Build Production Image

```bash
docker build -t crypto-alert-api:latest -f Dockerfile .
```

### 2. Docker Compose Production

Create `docker-compose.prod.yml`:

```yaml
version: '3.8'

services:
  api:
    image: crypto-alert-api:latest
    restart: always
    ports:
      - "8000:8000"
    environment:
      - ENVIRONMENT=production
      - DEBUG=false
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_URL=${REDIS_URL}
      - SECRET_KEY=${SECRET_KEY}
    depends_on:
      - postgres
      - redis
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/v1/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s

  postgres:
    image: postgres:16-alpine
    restart: always
    environment:
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: ${DB_NAME}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"

  redis:
    image: redis:7-alpine
    restart: always
    command: redis-server --appendonly yes --requirepass ${REDIS_PASSWORD}
    volumes:
      - redis_data:/data
    ports:
      - "6379:6379"

  nginx:
    image: nginx:alpine
    restart: always
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
      - ./ssl:/etc/nginx/ssl
    depends_on:
      - api

volumes:
  postgres_data:
  redis_data:
```

### 3. Nginx Configuration

Create `nginx.conf`:

```nginx
events {
    worker_connections 1024;
}

http {
    upstream api {
        server api:8000;
    }

    server {
        listen 80;
        server_name yourdomain.com;

        # Redirect to HTTPS
        return 301 https://$server_name$request_uri;
    }

    server {
        listen 443 ssl http2;
        server_name yourdomain.com;

        ssl_certificate /etc/nginx/ssl/cert.pem;
        ssl_certificate_key /etc/nginx/ssl/key.pem;

        # Rate limiting
        limit_req_zone $binary_remote_addr zone=api_limit:10m rate=100r/m;

        location / {
            proxy_pass http://api;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;

            limit_req zone=api_limit burst=20 nodelay;
        }

        location /api/v1/health {
            proxy_pass http://api;
            access_log off;
        }
    }
}
```

### 4. Start Production Stack

```bash
# Set environment variables
export DATABASE_URL="postgresql+asyncpg://user:pass@postgres:5432/cryptoalert"
export REDIS_URL="redis://:password@redis:6379/0"
export SECRET_KEY="your-production-secret-key"
export DB_USER="cryptoalert"
export DB_PASSWORD="secure-password"
export DB_NAME="cryptoalert"
export REDIS_PASSWORD="secure-redis-password"

# Start services
docker-compose -f docker-compose.prod.yml up -d

# Check logs
docker-compose -f docker-compose.prod.yml logs -f api
```

## Cloud Deployment

### AWS Deployment

#### 1. RDS PostgreSQL Setup

```bash
# Create RDS instance
aws rds create-db-instance \
  --db-instance-identifier crypto-alert-db \
  --db-instance-class db.t3.micro \
  --engine postgres \
  --engine-version 16.1 \
  --master-username admin \
  --master-user-password YourSecurePassword \
  --allocated-storage 20 \
  --backup-retention-period 7
```

#### 2. ElastiCache Redis

```bash
# Create Redis cluster
aws elasticache create-cache-cluster \
  --cache-cluster-id crypto-alert-cache \
  --cache-node-type cache.t3.micro \
  --engine redis \
  --num-cache-nodes 1
```

#### 3. ECS/Fargate Deployment

Create `task-definition.json`:

```json
{
  "family": "crypto-alert-api",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "containerDefinitions": [
    {
      "name": "api",
      "image": "your-ecr-repo/crypto-alert-api:latest",
      "portMappings": [
        {
          "containerPort": 8000,
          "protocol": "tcp"
        }
      ],
      "environment": [
        {
          "name": "ENVIRONMENT",
          "value": "production"
        }
      ],
      "secrets": [
        {
          "name": "DATABASE_URL",
          "valueFrom": "arn:aws:secretsmanager:region:account:secret:database-url"
        },
        {
          "name": "SECRET_KEY",
          "valueFrom": "arn:aws:secretsmanager:region:account:secret:secret-key"
        }
      ],
      "healthCheck": {
        "command": ["CMD-SHELL", "curl -f http://localhost:8000/api/v1/health || exit 1"],
        "interval": 30,
        "timeout": 5,
        "retries": 3
      },
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/crypto-alert-api",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ]
}
```

Deploy:

```bash
# Register task definition
aws ecs register-task-definition --cli-input-json file://task-definition.json

# Create service
aws ecs create-service \
  --cluster crypto-alert-cluster \
  --service-name crypto-alert-api \
  --task-definition crypto-alert-api \
  --desired-count 2 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-xxx],securityGroups=[sg-xxx],assignPublicIp=ENABLED}"
```

### GCP Deployment (Cloud Run)

```bash
# Build and push image
gcloud builds submit --tag gcr.io/PROJECT_ID/crypto-alert-api

# Deploy to Cloud Run
gcloud run deploy crypto-alert-api \
  --image gcr.io/PROJECT_ID/crypto-alert-api \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --max-instances 10 \
  --set-env-vars ENVIRONMENT=production \
  --set-secrets DATABASE_URL=database-url:latest,SECRET_KEY=secret-key:latest
```

## Monitoring Setup

### 1. Prometheus Configuration

Create `monitoring/prometheus.yml`:

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: 'crypto-alert-api'
    static_configs:
      - targets: ['api:9090']
    metrics_path: '/metrics'
```

### 2. Grafana Setup

```bash
# Add Prometheus datasource
curl -X POST http://admin:admin@localhost:3000/api/datasources \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Prometheus",
    "type": "prometheus",
    "url": "http://prometheus:9090",
    "access": "proxy",
    "isDefault": true
  }'
```

### 3. Alerting Rules

Create `monitoring/alert.rules`:

```yaml
groups:
  - name: crypto_alert_api
    interval: 30s
    rules:
      - alert: HighLatency
        expr: histogram_quantile(0.99, http_request_duration_seconds_bucket) > 0.5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High API latency detected"

      - alert: HighErrorRate
        expr: rate(http_requests_total{status_code=~"5.."}[5m]) > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "High error rate detected"

      - alert: WebSocketDisconnected
        expr: websocket_connections_active == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "All WebSocket connections lost"
```

## Security Checklist

### Production Security

- [ ] Generate strong `SECRET_KEY` (32+ random bytes)
- [ ] Use HTTPS/TLS for all connections
- [ ] Enable database SSL/TLS
- [ ] Set up firewall rules (only necessary ports)
- [ ] Use environment variables for secrets (never commit)
- [ ] Enable rate limiting (API + nginx)
- [ ] Set up CORS properly (whitelist origins)
- [ ] Regular security updates (dependencies)
- [ ] Enable database backups
- [ ] Implement API key rotation policy
- [ ] Set up intrusion detection (fail2ban)
- [ ] Enable audit logging
- [ ] Use secrets manager (AWS Secrets Manager, GCP Secret Manager)

### Nginx Security Headers

Add to nginx config:

```nginx
add_header X-Frame-Options "SAMEORIGIN" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-XSS-Protection "1; mode=block" always;
add_header Referrer-Policy "no-referrer-when-downgrade" always;
add_header Content-Security-Policy "default-src 'self' http: https: data: blob: 'unsafe-inline'" always;
```

## Scaling Strategy

### Vertical Scaling

For single-instance deployments:

| Users | CPU | RAM | Database | Redis |
|-------|-----|-----|----------|-------|
| 100 | 1 core | 2GB | db.t3.micro | cache.t3.micro |
| 500 | 2 cores | 4GB | db.t3.small | cache.t3.small |
| 1000+ | 4 cores | 8GB | db.t3.medium | cache.t3.medium |

### Horizontal Scaling

For high availability:

```yaml
# docker-compose.scale.yml
services:
  api:
    deploy:
      replicas: 3
      resources:
        limits:
          cpus: '1'
          memory: 2G
        reservations:
          cpus: '0.5'
          memory: 1G
```

Run:

```bash
docker-compose -f docker-compose.prod.yml up -d --scale api=3
```

### Database Scaling

#### Read Replicas

```python
# config.py
DATABASE_READ_URL = "postgresql+asyncpg://user:pass@read-replica:5432/db"

# Use read replica for queries
async def list_alerts():
    engine = create_async_engine(DATABASE_READ_URL)
    # ... query logic
```

#### Connection Pooling

```python
# Adjust pool size based on load
DATABASE_POOL_SIZE = 20
DATABASE_MAX_OVERFLOW = 10
```

### Redis Scaling

#### Redis Cluster

For > 10K concurrent users:

```bash
# Create Redis cluster
docker run -d --name redis-cluster \
  -p 7000-7005:7000-7005 \
  redis:7-alpine redis-cli --cluster create \
  redis://node1:7000 redis://node2:7001 redis://node3:7002 \
  --cluster-replicas 1
```

## Maintenance

### Backup Strategy

```bash
# Database backup
pg_dump -h localhost -U postgres cryptoalert > backup_$(date +%Y%m%d).sql

# Automated backups (cron)
0 2 * * * /usr/local/bin/backup-db.sh
```

### Log Rotation

```bash
# /etc/logrotate.d/crypto-alert-api
/var/log/crypto-alert-api/*.log {
    daily
    rotate 7
    compress
    delaycompress
    notifempty
    create 0640 www-data www-data
    sharedscripts
    postrotate
        docker-compose -f /opt/crypto-alert-api/docker-compose.prod.yml kill -s HUP api
    endscript
}
```

### Updates

```bash
# Pull latest image
docker pull crypto-alert-api:latest

# Zero-downtime update
docker-compose -f docker-compose.prod.yml up -d --no-deps --build api
```

## Troubleshooting

### Check Service Health

```bash
# API health
curl http://localhost:8000/api/v1/health

# Check logs
docker-compose logs -f api

# Database connection
docker-compose exec postgres psql -U postgres -d cryptoalert

# Redis connection
docker-compose exec redis redis-cli ping
```

### Common Issues

**High latency:**
```bash
# Check Redis
redis-cli --latency

# Check database slow queries
docker-compose exec postgres psql -U postgres -c "SELECT * FROM pg_stat_statements ORDER BY total_time DESC LIMIT 10;"
```

**WebSocket disconnections:**
```bash
# Check exchange connectivity
curl -I https://stream.binance.com:9443

# Restart connections
docker-compose restart api
```

## Support

For deployment issues:
- Open GitHub issue
- Check documentation: `/docs`
- Monitor logs and metrics
