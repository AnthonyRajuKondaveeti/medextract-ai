# MedExtract - Docker Deployment Guide

Complete Docker-based deployment with PostgreSQL included.

## 🚀 Quick Start

### Prerequisites
- Docker Desktop installed (Windows/Mac/Linux)
- OpenAI API key

### 1. Configure Environment

Create `.env.docker` file:
```bash
cp .env.docker.template .env.docker
```

Edit `.env.docker` and add your **OpenAI API key**:
```env
OPENAI_API_KEY=sk-proj-your-actual-key-here
```

### 2. Start Everything

```bash
# Build and start all services (app + database)
docker-compose --env-file .env.docker up -d

# View logs
docker-compose logs -f

# Check status
docker-compose ps
```

### 3. Access the Application

Open your browser: **http://localhost:8000**

Upload your PDF documents and download the Excel report!

---

## 📋 Available Commands

### Start Services
```bash
# Start in background
docker-compose --env-file .env.docker up -d

# Start with logs visible
docker-compose --env-file .env.docker up

# Rebuild and start (after code changes)
docker-compose --env-file .env.docker up -d --build
```

### Stop Services
```bash
# Stop services (keeps data)
docker-compose down

# Stop and remove all data
docker-compose down -v
```

### View Logs
```bash
# All services
docker-compose logs -f

# Just the app
docker-compose logs -f app

# Just the database
docker-compose logs -f db
```

### Restart Services
```bash
# Restart everything
docker-compose restart

# Restart just the app
docker-compose restart app
```

### Check Status
```bash
docker-compose ps
```

---

## 🗄️ Database Access

### Connect to PostgreSQL

From host machine:
```bash
psql -h localhost -p 5432 -U med -d medextract
# Password: secret
```

From Docker:
```bash
docker-compose exec db psql -U med -d medextract
```

### Backup Database
```bash
docker-compose exec db pg_dump -U med medextract > backup.sql
```

### Restore Database
```bash
cat backup.sql | docker-compose exec -T db psql -U med -d medextract
```

---

## 🔧 Configuration

### Change Ports

Edit `docker-compose.yml`:
```yaml
services:
  app:
    ports:
      - "9000:8000"  # Change 9000 to your preferred port
```

### Change Database Credentials

Edit `docker-compose.yml`:
```yaml
services:
  db:
    environment:
      POSTGRES_PASSWORD: your_secure_password
  app:
    environment:
      DATABASE_URL: postgresql://med:your_secure_password@db:5432/medextract
```

### Adjust OCR Confidence

Edit `.env.docker`:
```env
OCR_CONFIDENCE_THRESHOLD=0.70  # Lower = more AI calls
```

---

## 📊 Monitoring

### Resource Usage
```bash
docker stats
```

### Application Health
```bash
curl http://localhost:8000/
```

### Database Health
```bash
docker-compose exec db pg_isready -U med
```

---

## 🔍 Troubleshooting

### Port Already in Use
```bash
# Check what's using port 8000
netstat -ano | findstr :8000  # Windows
lsof -i :8000                 # Mac/Linux

# Change port in docker-compose.yml
```

### Database Connection Failed
```bash
# Check database is running
docker-compose ps db

# View database logs
docker-compose logs db

# Restart database
docker-compose restart db
```

### Application Crashes
```bash
# View application logs
docker-compose logs app

# Check environment variables
docker-compose exec app env | grep -E 'OPENAI|DATABASE'

# Restart application
docker-compose restart app
```

### Clear Everything and Start Fresh
```bash
# Stop all services
docker-compose down -v

# Remove images
docker-compose down --rmi all

# Rebuild from scratch
docker-compose --env-file .env.docker up -d --build
```

---

## 📦 Data Persistence

All data is persisted in Docker volumes:

- **postgres_data**: Database files (survives container restarts)
- **./uploads**: Temporary PDF uploads
- **./output**: Generated Excel files

### Backup All Data
```bash
# Backup database
docker-compose exec db pg_dump -U med medextract > backup.sql

# Your files are already in ./uploads and ./output folders
```

---

## 🚀 Production Deployment

### Security Checklist

1. **Change API_KEY**:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

2. **Change Database Password** in `docker-compose.yml`

3. **Use Environment Variables** (don't commit `.env.docker`)

4. **Enable HTTPS** (use nginx reverse proxy)

5. **Set ENV=production** in `.env.docker`

### Production Docker Compose

Add to `docker-compose.yml`:
```yaml
services:
  app:
    restart: always
    environment:
      ENV: production
```

---

## 📈 Scaling

### Multiple Workers

Edit `docker-compose.yml`:
```yaml
services:
  app:
    deploy:
      replicas: 3
```

### External Database

Use managed PostgreSQL (AWS RDS, Azure, etc.):
```yaml
services:
  app:
    environment:
      DATABASE_URL: postgresql://user:pass@external-db.com:5432/medextract
  # Remove db service
```

---

## 🛠️ Development

### Hot Reload (for development)

Edit `docker-compose.yml`:
```yaml
services:
  app:
    volumes:
      - .:/app
    command: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Access Container Shell
```bash
docker-compose exec app bash
```

---

## ✅ System Requirements

**Minimum:**
- 2 CPU cores
- 4 GB RAM
- 10 GB disk space

**Recommended:**
- 4 CPU cores
- 8 GB RAM
- 20 GB disk space

---

## 📞 Support

If you encounter issues:

1. Check logs: `docker-compose logs -f`
2. Verify environment: `docker-compose exec app env`
3. Test database: `docker-compose exec db pg_isready -U med`
4. Restart services: `docker-compose restart`

---

## 🎯 What's Included

- ✅ **Python 3.12** application
- ✅ **PostgreSQL 16** database
- ✅ **Tesseract OCR** (lightweight, faster build - PaddleOCR removed)
- ✅ **Poppler** for PDF processing
- ✅ **OpenAI GPT-4o** integration
- ✅ **Health checks** for both services
- ✅ **Automatic restarts** on failure
- ✅ **Data persistence** with volumes
- ✅ **Network isolation** for security

Ready to process medical reports at scale! 🚀
