#!/bin/bash
# MedExtract Docker Startup Script

set -e

echo "═══════════════════════════════════════════════════════"
echo "  MedExtract - Medical Report Intelligence Platform"
echo "═══════════════════════════════════════════════════════"
echo ""

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "❌ Error: Docker is not installed"
    echo "   Please install Docker Desktop from: https://www.docker.com/products/docker-desktop"
    exit 1
fi

# Check if Docker is running
if ! docker info &> /dev/null; then
    echo "❌ Error: Docker is not running"
    echo "   Please start Docker Desktop"
    exit 1
fi

echo "✓ Docker is installed and running"
echo ""

# Check if .env.docker exists
if [ ! -f .env.docker ]; then
    echo "📝 Creating .env.docker file..."
    cp .env.docker.template .env.docker
    echo ""
    echo "⚠️  IMPORTANT: Please edit .env.docker and add your OpenAI API key!"
    echo ""
    echo "   Open .env.docker and set:"
    echo "   OPENAI_API_KEY=sk-proj-your-actual-key-here"
    echo ""
    read -p "Press Enter after you've updated the API key..."
fi

echo "🚀 Starting MedExtract services..."
echo ""

# Stop any existing containers
docker-compose down 2>/dev/null || true

# Build and start services
docker-compose --env-file .env.docker up -d --build

echo ""
echo "⏳ Waiting for services to be ready..."
sleep 5

# Check if services are running
if docker-compose ps | grep -q "Up"; then
    echo ""
    echo "✅ MedExtract is running!"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  🌐 Web UI:      http://localhost:8000"
    echo "  📊 Database:    localhost:5432"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "📖 Useful commands:"
    echo "   View logs:       docker-compose logs -f"
    echo "   Stop services:   docker-compose down"
    echo "   Restart:         docker-compose restart"
    echo ""
else
    echo ""
    echo "❌ Error: Services failed to start"
    echo "   View logs: docker-compose logs"
    exit 1
fi
