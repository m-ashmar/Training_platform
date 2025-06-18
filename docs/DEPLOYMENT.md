# Deployment Guide

## Overview

This guide provides comprehensive instructions for deploying the Training Platform to production environments. The platform supports multiple deployment scenarios including traditional server deployment, containerized deployment, and cloud platform deployment.

## Prerequisites

### System Requirements

- **Operating System**: Ubuntu 20.04+ / CentOS 8+ / Debian 11+
- **Python**: 3.8 or higher
- **Database**: PostgreSQL 12+ or MySQL 8+
- **Redis**: 6.0+ for caching and session management
- **Web Server**: Nginx 1.18+ or Apache 2.4+
- **SSL Certificate**: Valid SSL certificate for HTTPS

### Software Dependencies

```bash
# Update system packages
sudo apt update && sudo apt upgrade -y

# Install Python and pip
sudo apt install python3 python3-pip python3-venv -y

# Install PostgreSQL
sudo apt install postgresql postgresql-contrib -y

# Install Redis
sudo apt install redis-server -y

# Install Nginx
sudo apt install nginx -y

# Install additional dependencies
sudo apt install build-essential libpq-dev python3-dev -y
```

## Environment Setup

### Create Application User

```bash
# Create dedicated user for the application
sudo useradd -m -s /bin/bash training_app
sudo usermod -aG sudo training_app

# Switch to application user
sudo su - training_app
```

### Python Environment

```bash
# Create virtual environment
python3 -m venv /home/training_app/venv
source /home/training_app/venv/bin/activate

# Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

### Database Configuration

#### PostgreSQL Setup

```bash
# Connect to PostgreSQL as postgres user
sudo -u postgres psql

# Create database and user
CREATE DATABASE training_platform;
CREATE USER training_user WITH PASSWORD 'secure_password';
GRANT ALL PRIVILEGES ON DATABASE training_platform TO training_user;
ALTER USER training_user CREATEDB;
\q
```

#### MySQL Setup

```bash
# Connect to MySQL
sudo mysql -u root -p

# Create database and user
CREATE DATABASE training_platform CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'training_user'@'localhost' IDENTIFIED BY 'secure_password';
GRANT ALL PRIVILEGES ON training_platform.* TO 'training_user'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

## Application Configuration

### Environment Variables

Create a production environment file:

```bash
# Create environment file
nano /home/training_app/training_platform/.env
```

```env
# Django Settings
DEBUG=False
SECRET_KEY=your_very_secure_secret_key_here
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com

# Database Configuration
DATABASE_URL=postgresql://training_user:secure_password@localhost:5432/training_platform

# Redis Configuration
REDIS_URL=redis://localhost:6379/0

# Payment Gateway Configuration
SYRIATEL_API_KEY=your_syriatel_api_key
SYRIATEL_SECRET_KEY=your_syriatel_secret_key
SYRIATEL_WEBHOOK_SECRET=your_webhook_secret

ALBARAKA_API_KEY=your_albaraka_api_key
ALBARAKA_SECRET_KEY=your_albaraka_secret_key
ALBARAKA_WEBHOOK_SECRET=your_webhook_secret

BEMO_API_KEY=your_bemo_api_key
BEMO_SECRET_KEY=your_bemo_secret_key
BEMO_WEBHOOK_SECRET=your_webhook_secret

# Webhook Configuration
WEBHOOK_BASE_URL=https://yourdomain.com/webhooks
WEBHOOK_TIMEOUT=30

# Email Configuration
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=your_email@gmail.com
EMAIL_HOST_PASSWORD=your_app_password

# Static Files
STATIC_ROOT=/home/training_app/staticfiles
MEDIA_ROOT=/home/training_app/media

# Security Settings
CSRF_COOKIE_SECURE=True
SESSION_COOKIE_SECURE=True
SECURE_BROWSER_XSS_FILTER=True
SECURE_CONTENT_TYPE_NOSNIFF=True
X_FRAME_OPTIONS=DENY
```

### Django Settings

Update the production settings:

```python
# training_platform/settings/production.py

import os
from .base import *

# Security Settings
DEBUG = False
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', '').split(',')

# Database Configuration
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'training_platform',
        'USER': 'training_user',
        'PASSWORD': 'secure_password',
        'HOST': 'localhost',
        'PORT': '5432',
    }
}

# Static Files Configuration
STATIC_ROOT = os.environ.get('STATIC_ROOT', '/home/training_app/staticfiles')
MEDIA_ROOT = os.environ.get('MEDIA_ROOT', '/home/training_app/media')

# Security Headers
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# SSL Configuration
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True

# Logging Configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'level': 'INFO',
            'class': 'logging.FileHandler',
            'filename': '/home/training_app/logs/django.log',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['file'],
        'level': 'INFO',
    },
}
```

## Database Migration

```bash
# Activate virtual environment
source /home/training_app/venv/bin/activate

# Run migrations
cd /home/training_app/training_platform
python manage.py migrate

# Create superuser
python manage.py createsuperuser

# Collect static files
python manage.py collectstatic --noinput
```

## Gunicorn Configuration

### Gunicorn Service File

```bash
# Create systemd service file
sudo nano /etc/systemd/system/training_platform.service
```

```ini
[Unit]
Description=Training Platform Gunicorn daemon
After=network.target

[Service]
User=training_app
Group=training_app
WorkingDirectory=/home/training_app/training_platform
Environment="PATH=/home/training_app/venv/bin"
ExecStart=/home/training_app/venv/bin/gunicorn --workers 3 --bind unix:/home/training_app/training_platform.sock training_platform.wsgi:application
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=5
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### Start Gunicorn Service

```bash
# Enable and start service
sudo systemctl enable training_platform
sudo systemctl start training_platform
sudo systemctl status training_platform
```

## Nginx Configuration

### Nginx Site Configuration

```bash
# Create Nginx configuration
sudo nano /etc/nginx/sites-available/training_platform
```

```nginx
server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com www.yourdomain.com;

    # SSL Configuration
    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-RSA-AES256-GCM-SHA512:DHE-RSA-AES256-GCM-SHA512:ECDHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;

    # Security Headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer-when-downgrade" always;
    add_header Content-Security-Policy "default-src 'self' http: https: data: blob: 'unsafe-inline'" always;

    # Client Max Body Size
    client_max_body_size 100M;

    # Static Files
    location /static/ {
        alias /home/training_app/staticfiles/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # Media Files
    location /media/ {
        alias /home/training_app/media/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # Proxy to Gunicorn
    location / {
        proxy_pass http://unix:/home/training_app/training_platform.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
    }

    # Webhook Endpoints (No CSRF)
    location /webhooks/ {
        proxy_pass http://unix:/home/training_app/training_platform.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Enable Nginx Site

```bash
# Enable site
sudo ln -s /etc/nginx/sites-available/training_platform /etc/nginx/sites-enabled/

# Test configuration
sudo nginx -t

# Restart Nginx
sudo systemctl restart nginx
```

## SSL Certificate

### Let's Encrypt Setup

```bash
# Install Certbot
sudo apt install certbot python3-certbot-nginx -y

# Obtain SSL certificate
sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com

# Set up auto-renewal
sudo crontab -e
# Add this line: 0 12 * * * /usr/bin/certbot renew --quiet
```

## Celery Configuration

### Celery Service File

```bash
# Create Celery service file
sudo nano /etc/systemd/system/training_platform_celery.service
```

```ini
[Unit]
Description=Training Platform Celery Worker
After=network.target

[Service]
Type=forking
User=training_app
Group=training_app
EnvironmentFile=/home/training_app/training_platform/.env
WorkingDirectory=/home/training_app/training_platform
ExecStart=/bin/sh -c '${WorkingDirectory}/venv/bin/celery multi start worker1 \
  -A training_platform --pidfile=${WorkingDirectory}/celery/%n.pid \
  --logfile=${WorkingDirectory}/celery/%n%I.log --loglevel=INFO'
ExecStop=/bin/sh -c '${WorkingDirectory}/venv/bin/celery multi stopwait worker1 \
  --pidfile=${WorkingDirectory}/celery/%n.pid'
ExecReload=/bin/sh -c '${WorkingDirectory}/venv/bin/celery multi restart worker1 \
  -A training_platform --pidfile=${WorkingDirectory}/celery/%n.pid \
  --logfile=${WorkingDirectory}/celery/%n%I.log --loglevel=INFO'

[Install]
WantedBy=multi-user.target
```

### Start Celery Service

```bash
# Create Celery directory
mkdir -p /home/training_app/training_platform/celery

# Enable and start service
sudo systemctl enable training_platform_celery
sudo systemctl start training_platform_celery
```

## Monitoring and Logging

### Log Rotation

```bash
# Configure log rotation
sudo nano /etc/logrotate.d/training_platform
```

```
/home/training_app/logs/*.log {
    daily
    missingok
    rotate 52
    compress
    delaycompress
    notifempty
    create 644 training_app training_app
    postrotate
        systemctl reload training_platform
    endscript
}
```

### Health Check Script

```bash
# Create health check script
nano /home/training_app/health_check.sh
```

```bash
#!/bin/bash

# Check if Django is running
if ! curl -f http://localhost:8000/health/ > /dev/null 2>&1; then
    echo "Django application is not responding"
    exit 1
fi

# Check database connectivity
cd /home/training_app/training_platform
source venv/bin/activate
if ! python manage.py check --database default > /dev/null 2>&1; then
    echo "Database connection failed"
    exit 1
fi

# Check Redis connectivity
if ! redis-cli ping > /dev/null 2>&1; then
    echo "Redis is not responding"
    exit 1
fi

echo "All services are healthy"
exit 0
```

```bash
# Make script executable
chmod +x /home/training_app/health_check.sh
```

## Backup Strategy

### Database Backup

```bash
# Create backup script
nano /home/training_app/backup.sh
```

```bash
#!/bin/bash

# Set variables
BACKUP_DIR="/home/training_app/backups"
DATE=$(date +%Y%m%d_%H%M%S)
DB_NAME="training_platform"

# Create backup directory
mkdir -p $BACKUP_DIR

# Database backup
pg_dump $DB_NAME > $BACKUP_DIR/db_backup_$DATE.sql

# Media files backup
tar -czf $BACKUP_DIR/media_backup_$DATE.tar.gz /home/training_app/media/

# Keep only last 7 days of backups
find $BACKUP_DIR -name "*.sql" -mtime +7 -delete
find $BACKUP_DIR -name "*.tar.gz" -mtime +7 -delete

echo "Backup completed: $DATE"
```

```bash
# Make script executable
chmod +x /home/training_app/backup.sh

# Add to crontab for daily backups
crontab -e
# Add this line: 0 2 * * * /home/training_app/backup.sh
```

## Security Hardening

### Firewall Configuration

```bash
# Configure UFW firewall
sudo ufw allow ssh
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

### Fail2ban Configuration

```bash
# Install Fail2ban
sudo apt install fail2ban -y

# Configure Fail2ban
sudo nano /etc/fail2ban/jail.local
```

```ini
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 3

[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 3

[nginx-http-auth]
enabled = true
filter = nginx-http-auth
logpath = /var/log/nginx/error.log
maxretry = 3
```

```bash
# Restart Fail2ban
sudo systemctl restart fail2ban
```

## Performance Optimization

### Database Optimization

```sql
-- PostgreSQL optimization
ALTER SYSTEM SET shared_buffers = '256MB';
ALTER SYSTEM SET effective_cache_size = '1GB';
ALTER SYSTEM SET maintenance_work_mem = '64MB';
ALTER SYSTEM SET checkpoint_completion_target = 0.9;
ALTER SYSTEM SET wal_buffers = '16MB';
ALTER SYSTEM SET default_statistics_target = 100;

-- Reload configuration
SELECT pg_reload_conf();
```

### Nginx Optimization

```nginx
# Add to nginx.conf
worker_processes auto;
worker_connections 1024;
keepalive_timeout 65;
gzip on;
gzip_types text/plain text/css application/json application/javascript text/xml application/xml application/xml+rss text/javascript;
```

## Troubleshooting

### Common Issues

1. **Permission Errors**
   ```bash
   sudo chown -R training_app:training_app /home/training_app/
   sudo chmod -R 755 /home/training_app/
   ```

2. **Database Connection Issues**
   ```bash
   # Check PostgreSQL status
   sudo systemctl status postgresql
   
   # Check connection
   psql -h localhost -U training_user -d training_platform
   ```

3. **Static Files Not Loading**
   ```bash
   # Recollect static files
   python manage.py collectstatic --noinput
   
   # Check Nginx configuration
   sudo nginx -t
   ```

### Log Analysis

```bash
# View Django logs
tail -f /home/training_app/logs/django.log

# View Nginx logs
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log

# View system logs
sudo journalctl -u training_platform -f
```

## Maintenance

### Regular Maintenance Tasks

```bash
# Update system packages
sudo apt update && sudo apt upgrade -y

# Update Python packages
source /home/training_app/venv/bin/activate
pip install --upgrade -r requirements.txt

# Restart services
sudo systemctl restart training_platform
sudo systemctl restart training_platform_celery
sudo systemctl restart nginx
```

### Monitoring Commands

```bash
# Check service status
sudo systemctl status training_platform
sudo systemctl status training_platform_celery
sudo systemctl status nginx
sudo systemctl status postgresql
sudo systemctl status redis

# Check disk usage
df -h

# Check memory usage
free -h

# Check process status
ps aux | grep gunicorn
ps aux | grep celery
```

This deployment guide provides a comprehensive approach to deploying the Training Platform in a production environment with proper security, monitoring, and maintenance procedures. 