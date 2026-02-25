# MedExtract Docker Commands

.PHONY: help start stop restart logs build clean status db-backup db-restore

help:
	@echo "MedExtract Docker Commands"
	@echo ""
	@echo "  make start       - Start all services"
	@echo "  make stop        - Stop all services"
	@echo "  make restart     - Restart all services"
	@echo "  make logs        - View logs"
	@echo "  make build       - Rebuild containers"
	@echo "  make clean       - Stop and remove everything"
	@echo "  make status      - Show service status"
	@echo "  make db-backup   - Backup database"
	@echo "  make db-restore  - Restore database"

start:
	docker-compose --env-file .env.docker up -d

stop:
	docker-compose down

restart:
	docker-compose restart

logs:
	docker-compose logs -f

build:
	docker-compose --env-file .env.docker up -d --build

clean:
	docker-compose down -v --rmi all

status:
	docker-compose ps

db-backup:
	docker-compose exec db pg_dump -U med medextract > backup_$(shell date +%Y%m%d_%H%M%S).sql
	@echo "Database backed up to backup_*.sql"

db-restore:
	@echo "Restoring from backup.sql..."
	cat backup.sql | docker-compose exec -T db psql -U med -d medextract
