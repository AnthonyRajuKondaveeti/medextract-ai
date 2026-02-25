# MedExtract Docker Startup Script (PowerShell)

Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  MedExtract - Medical Report Intelligence Platform" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

# Check if Docker is installed
try {
    $null = docker --version
    Write-Host "✓ Docker is installed" -ForegroundColor Green
} catch {
    Write-Host "❌ Error: Docker is not installed" -ForegroundColor Red
    Write-Host "   Please install Docker Desktop from: https://www.docker.com/products/docker-desktop"
    exit 1
}

# Check if Docker is running
try {
    $null = docker info 2>&1
    Write-Host "✓ Docker is running" -ForegroundColor Green
} catch {
    Write-Host "❌ Error: Docker is not running" -ForegroundColor Red
    Write-Host "   Please start Docker Desktop"
    exit 1
}

Write-Host ""

# Check if .env.docker exists
if (-not (Test-Path .env.docker)) {
    Write-Host "📝 Creating .env.docker file..." -ForegroundColor Yellow
    Copy-Item .env.docker.template .env.docker
    Write-Host ""
    Write-Host "⚠️  IMPORTANT: Please edit .env.docker and add your OpenAI API key!" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "   Open .env.docker and set:" -ForegroundColor White
    Write-Host "   OPENAI_API_KEY=sk-proj-your-actual-key-here" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Press Enter after you've updated the API key..." -ForegroundColor Yellow
    Read-Host
}

Write-Host "🚀 Starting MedExtract services..." -ForegroundColor Cyan
Write-Host ""

# Stop any existing containers
docker-compose down 2>$null

# Build and start services
docker-compose --env-file .env.docker up -d --build

Write-Host ""
Write-Host "⏳ Waiting for services to be ready..." -ForegroundColor Yellow
Start-Sleep -Seconds 5

# Check if services are running
$running = docker-compose ps | Select-String "Up"
if ($running) {
    Write-Host ""
    Write-Host "✅ MedExtract is running!" -ForegroundColor Green
    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host "  🌐 Web UI:      http://localhost:8000" -ForegroundColor White
    Write-Host "  📊 Database:    localhost:5432" -ForegroundColor White
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "📖 Useful commands:" -ForegroundColor Yellow
    Write-Host "   View logs:       docker-compose logs -f" -ForegroundColor White
    Write-Host "   Stop services:   docker-compose down" -ForegroundColor White
    Write-Host "   Restart:         docker-compose restart" -ForegroundColor White
    Write-Host ""
    
    # Ask if user wants to open browser
    $open = Read-Host "Open web browser? (Y/n)"
    if ($open -ne "n" -and $open -ne "N") {
        Start-Process "http://localhost:8000"
    }
} else {
    Write-Host ""
    Write-Host "❌ Error: Services failed to start" -ForegroundColor Red
    Write-Host "   View logs: docker-compose logs" -ForegroundColor Yellow
    exit 1
}
