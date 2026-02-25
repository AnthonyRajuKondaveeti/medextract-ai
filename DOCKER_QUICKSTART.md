# 🐳 Quick Start with Docker

Run everything (app + database) with Docker in 3 steps!

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop) installed
- OpenAI API key from [platform.openai.com](https://platform.openai.com/api-keys)

---

## 🚀 Steps

### 1️⃣ Configure API Key

**Windows (PowerShell):**
```powershell
# Copy template
Copy-Item .env.docker.template .env.docker

# Edit with notepad
notepad .env.docker
```

**Mac/Linux:**
```bash
# Copy template
cp .env.docker.template .env.docker

# Edit with your favorite editor
nano .env.docker
```

Add your OpenAI API key:
```env
OPENAI_API_KEY=sk-proj-your-actual-key-here
```

### 2️⃣ Start Services

**Windows (PowerShell):**
```powershell
.\start.ps1
```

**Mac/Linux:**
```bash
chmod +x start.sh
./start.sh
```

**Or manually:**
```bash
docker-compose --env-file .env.docker up -d
```

### 3️⃣ Use the Application

Open your browser: **http://localhost:8000**

🎉 That's it! Upload your PDF documents and get your Excel report!

---

## 📋 Common Commands

```bash
# View logs
docker-compose logs -f

# Stop everything
docker-compose down

# Restart
docker-compose restart

# Check status
docker-compose ps

# Stop and remove all data
docker-compose down -v
```

---

## 🔍 Troubleshooting

### Port 8000 already in use?
```yaml
# Edit docker-compose.yml, change port:
services:
  app:
    ports:
      - "9000:8000"  # Use port 9000 instead
```

### Can't connect to database?
```bash
# Check database logs
docker-compose logs db

# Restart database
docker-compose restart db
```

### Application not starting?
```bash
# View application logs
docker-compose logs app

# Rebuild from scratch
docker-compose down -v
docker-compose --env-file .env.docker up -d --build
```

---

## 📖 Full Documentation

See [DOCKER_GUIDE.md](DOCKER_GUIDE.md) for complete documentation including:
- Production deployment
- Database management
- Scaling options
- Security hardening
- Monitoring and troubleshooting

---

## 🎯 What's Running?

- **Web Application**: http://localhost:8000
- **PostgreSQL Database**: localhost:5432
  - Database: `medextract`
  - User: `med`
  - Password: `secret`

---

## ✅ System Requirements

**Minimum:**
- Docker Desktop with 4 GB RAM allocated
- 10 GB disk space

**Recommended:**
- Docker Desktop with 8 GB RAM allocated
- 20 GB disk space

---

Easy! 🚀
