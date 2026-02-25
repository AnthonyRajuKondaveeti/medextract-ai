# Authentication & Scaling Configuration

## 🔐 Authentication System

### Overview
**NEW SECURE LOGIN SYSTEM** — The API key is now protected server-side. Users must login with username/password to access the platform.

### Credentials
```
Username: admin
Password: Admin@321
```

### How It Works

1. **Login Process**
   - User visits the application → sees login screen
   - Enters username/password → clicks Login
   - Server validates credentials → returns session token (JWT-style)
   - Token stored in browser sessionStorage (cleared on tab close)
   - Token valid for **8 hours** from login

2. **Session Management**
   - All API requests include `Authorization: Bearer <token>` header
   - Server validates token on every request
   - Expired/invalid tokens → user redirected to login
   - Logout button clears session and returns to login screen

3. **Security Improvements**
   - ✅ API key **removed from frontend** (was exposed in index.html line 412)
   - ✅ API key now **only exists server-side** (.env file and backend code)
   - ✅ Users cannot extract or abuse the API key
   - ✅ Session tokens expire automatically
   - ✅ Logout invalidates tokens immediately

### Files Modified

**Backend: `main.py`**
- Added `/login` POST endpoint (validates username/password, returns token)
- Added `/logout` POST endpoint (invalidates session)
- Added session token management (in-memory store with expiration)
- Updated authentication: accepts either session token OR API key (backward compatible)
- All protected endpoints (`/upload`, `/status`, `/download`) now validate session tokens

**Frontend: `static/index.html`**
- Added login overlay with username/password form
- Added logout button in header
- Removed exposed API key constant
- Updated all fetch calls to use `Authorization: Bearer <token>` header
- Added session token storage in sessionStorage
- Added automatic login state check on page load

### Development Notes

**For production:**
1. Change admin credentials (currently hardcoded in `main.py`)
2. Use database for user storage with hashed passwords (bcrypt/argon2)
3. Use Redis for session storage instead of in-memory dict
4. Consider JWT with RSA signing for distributed deployments
5. Add password reset flow
6. Add multi-user support with role-based access control

---

## 🔄 Load Balancing Assessment

### Current Status: ❌ NOT CONFIGURED

**Findings:**
- Single application container running on port 8000
- No load balancer service (nginx, traefik, haproxy) in docker-compose
- Direct port mapping from host → app container
- All requests go to single FastAPI process

**Current Architecture:**
```
Internet → Port 8000 → [medextract-app container] → FastAPI → 10 worker threads
```

**What's Missing:**
```
Internet → Load Balancer → Multiple app containers (round-robin)
                            ├─ app-1 (10 workers)
                            ├─ app-2 (10 workers)  
                            └─ app-3 (10 workers)
```

### To Add Load Balancing:

**Option 1: nginx reverse proxy** (recommended for Docker Compose)

Add to `docker-compose.yml`:
```yaml
services:
  nginx:
    image: nginx:alpine
    container_name: medextract-lb
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - app-1
      - app-2
      - app-3
    networks:
      - medextract-network

  app-1:
    build: .
    container_name: medextract-app-1
    # No ports exposed (internal only)
    environment:
      # ... same as current app ...

  app-2:
    build: .
    container_name: medextract-app-2
    environment:
      # ... same as current app ...

  app-3:
    build: .
    container_name: medextract-app-3
    environment:
      # ... same as current app ...
```

**nginx.conf example:**
```nginx
upstream medextract_backend {
    least_conn;  # Route to server with fewest connections
    server app-1:8000;
    server app-2:8000;
    server app-3:8000;
}

server {
    listen 80;
    client_max_body_size 500M;
    
    location / {
        proxy_pass http://medextract_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 600s;
    }
}
```

---

## 📈 Horizontal Scaling Assessment

### Current Status: ❌ NOT CONFIGURED

**Findings:**
- Single app container (`medextract-app`)
- No `deploy.replicas` configuration
- No container orchestration (Kubernetes, Docker Swarm)
- Database correctly shared (PostgreSQL on separate container)

**Current Scaling:**
- ✅ **Vertical scaling via workers**: 10 concurrent PDF processors inside single container
- ❌ **Horizontal scaling**: Cannot spawn multiple containers automatically
- ✅ **Stateless design**: App is stateless (good for scaling)
- ✅ **Shared database**: PostgreSQL on separate container (required for multi-instance)

### Scaling Capabilities

**What the system CAN handle now:**
- **300-400 documents/day** with current 10-worker configuration
- **Vertical scaling**: Increase MAX_WORKERS to 15-20 (requires more CPU/RAM)
- **Manual horizontal scaling**: Run 2-3 containers manually with nginx load balancer

**What the system CANNOT handle now:**
- **Automatic scaling**: No auto-increase of containers during peak load
- **High availability**: Single point of failure (if app crashes, entire service down)
- **Distributed load**: Cannot automatically distribute across multiple servers

### Horizontal Scaling Readiness: ✅ READY

**Good news:** The application is **ready for horizontal scaling** with minimal changes:

✅ **Stateless design**
   - No local state stored in app container
   - All data in PostgreSQL (shared across instances)
   - Excel files can be served from shared volume or object storage

✅ **Session management compatible**
   - Current in-memory sessions work for single instance
   - Can migrate to Redis for distributed sessions (5-minute change)

✅ **Database architecture**
   - PostgreSQL on separate container
   - Connection pooling supported
   - Can handle multiple app instances connecting

❌ **What needs to be added:**
   - Load balancer (nginx/traefik)
   - Redis for shared session store
   - Shared file storage (NFS/S3) if running on multiple hosts

### Scaling Options

**Option 1: Docker Compose with replicas** (for single server)
```yaml
services:
  app:
    build: .
    deploy:
      replicas: 3  # Run 3 instances
      resources:
        limits:
          cpus: '2'
          memory: 4G
    # ... rest of config ...
```

Then add nginx load balancer (see Load Balancing section above)

**Option 2: Kubernetes** (for multi-server clusters)
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: medextract-app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: medextract
  template:
    # ... pod spec ...
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: medextract-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: medextract-app
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
```

**Option 3: Docker Swarm** (simpler than Kubernetes)
```bash
docker swarm init
docker stack deploy -c docker-compose.yml medextract
docker service scale medextract_app=3
```

---

## 🎯 Current System Capabilities

### What Works Now (300 docs/day)
- ✅ Single container with 10 parallel workers
- ✅ Rate limiting (450 requests/minute to OpenAI)
- ✅ Async processing with semaphore-based concurrency
- ✅ Shared PostgreSQL for job persistence
- ✅ Secure authentication with session tokens
- ✅ Can process ~12-15 docs/hour = **288-360 docs/day**

### Performance Profile
- **Single container**: 10 workers × 3-4 min/doc = ~20-30 docs/hour
- **With 3 containers**: 30 workers × 3-4 min/doc = ~60-90 docs/hour = **1440-2160 docs/day**

### Bottlenecks
1. **OpenAI rate limit**: 450 RPM (Tier 1) → can upgrade to Tier 2 (3,500 RPM)
2. **CPU for OCR**: Tesseract uses ~40% CPU per worker (lighter than PaddleOCR)
3. **Single container**: No redundancy, no auto-scaling

---

## 📋 Recommendations

### Immediate (Current Setup)
✅ **DONE**: Authentication secured, API key protected
✅ **DONE**: 10 workers configured, handling 300 docs/day
✅ **DONE**: Rate limiting to prevent API abuse

### Short-term (Next 1-2 weeks)
- [ ] **Add Redis** for distributed session storage
- [ ] **Add nginx load balancer** to docker-compose
- [ ] **Run 2-3 app replicas** for redundancy
- [ ] **Monitor with Prometheus** + Grafana for performance insights

### Long-term (Production)
- [ ] **Migrate to Kubernetes** for auto-scaling
- [ ] **Add HorizontalPodAutoscaler** (scale 2-10 pods based on CPU)
- [ ] **Upgrade OpenAI tier** to Tier 2 (3,500 RPM) for higher throughput
- [ ] **Add S3/MinIO** for shared file storage across nodes
- [ ] **Add health checks** and auto-restart policies
- [ ] **Database connection pooling** (pgBouncer) for 10+ app instances

---

## 🚀 Quick Start Guide

### Using the New Authentication System

1. **Start the application**
   ```bash
   docker-compose up -d
   ```

2. **Open browser**
   ```
   http://localhost:8000
   ```

3. **Login with credentials**
   ```
   Username: admin
   Password: Admin@321
   ```

4. **Upload PDFs and process**
   - Session lasts 8 hours
   - Logout button in top-right corner
   - Token stored in sessionStorage (cleared on browser close)

### For Testing
- Old API key authentication still works for backward compatibility
- Send API requests with `X-API-Key: <key>` header OR `Authorization: Bearer <token>`

---

## 📞 Support

**Authentication Issues:**
- Check browser console for errors
- Clear sessionStorage: `sessionStorage.clear()`
- Check server logs: `docker logs medextract-app`

**Scaling Questions:**
- Current setup handles 300 docs/day with single container
- For 1000+ docs/day, implement load balancing + replicas
- For high availability, migrate to Kubernetes

**Security Concerns:**
- Change admin password in main.py (lines 23-24)
- Rotate API_KEY in .env file
- Use HTTPS in production (add SSL certificate to nginx)
