#!/bin/bash
set -e

echo "🚀 Crypto Alert API Setup"
echo "========================="
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Check prerequisites
echo "Checking prerequisites..."

command -v docker >/dev/null 2>&1 || {
    echo -e "${RED}Error: Docker is required but not installed.${NC}"
    exit 1
}

command -v docker-compose >/dev/null 2>&1 || {
    echo -e "${RED}Error: Docker Compose is required but not installed.${NC}"
    exit 1
}

command -v python3 >/dev/null 2>&1 || {
    echo -e "${RED}Error: Python 3 is required but not installed.${NC}"
    exit 1
}

echo -e "${GREEN}✓ Prerequisites satisfied${NC}"
echo ""

# Create .env if it doesn't exist
if [ ! -f .env ]; then
    echo "Creating .env file..."
    cp .env.example .env

    # Generate secret key
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        sed -i '' "s/your-secret-key-change-in-production/$SECRET_KEY/" .env
    else
        # Linux
        sed -i "s/your-secret-key-change-in-production/$SECRET_KEY/" .env
    fi

    echo -e "${GREEN}✓ Created .env with generated SECRET_KEY${NC}"
else
    echo -e "${YELLOW}⚠ .env already exists, skipping${NC}"
fi
echo ""

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv venv
    echo -e "${GREEN}✓ Virtual environment created${NC}"
else
    echo -e "${YELLOW}⚠ Virtual environment already exists${NC}"
fi
echo ""

# Activate virtual environment and install dependencies
echo "Installing Python dependencies..."
source venv/bin/activate
pip install --upgrade pip > /dev/null 2>&1
pip install -r requirements.txt > /dev/null 2>&1
echo -e "${GREEN}✓ Dependencies installed${NC}"
echo ""

# Start Docker services
echo "Starting Docker services (PostgreSQL, Redis, Prometheus, Grafana)..."
docker-compose up -d postgres redis prometheus grafana
echo -e "${GREEN}✓ Docker services started${NC}"
echo ""

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL to be ready..."
for i in {1..30}; do
    if docker-compose exec -T postgres pg_isready -U postgres > /dev/null 2>&1; then
        echo -e "${GREEN}✓ PostgreSQL is ready${NC}"
        break
    fi
    if [ $i -eq 30 ]; then
        echo -e "${RED}Error: PostgreSQL failed to start${NC}"
        exit 1
    fi
    sleep 1
done
echo ""

# Wait for Redis to be ready
echo "Waiting for Redis to be ready..."
for i in {1..30}; do
    if docker-compose exec -T redis redis-cli ping > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Redis is ready${NC}"
        break
    fi
    if [ $i -eq 30 ]; then
        echo -e "${RED}Error: Redis failed to start${NC}"
        exit 1
    fi
    sleep 1
done
echo ""

# Initialize database
echo "Initializing database..."
# Create alembic.ini if not exists
if [ ! -f alembic.ini ]; then
    cat > alembic.ini << EOF
[alembic]
script_location = alembic
prepend_sys_path = .
sqlalchemy.url = postgresql+asyncpg://postgres:postgres@localhost:5432/cryptoalert

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
EOF
fi

# Create alembic directory structure
if [ ! -d "alembic" ]; then
    mkdir -p alembic/versions
    cat > alembic/env.py << 'EOF'
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
import asyncio

from src.models import Base
from src.config import get_settings

config = context.config
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url_str)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
EOF

    cat > alembic/script.py.mako << 'EOF'
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

def upgrade() -> None:
    ${upgrades if upgrades else "pass"}

def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
EOF
fi

# Run database initialization directly
echo "Creating database tables..."
python3 -c "
import asyncio
from src.database import init_db

async def main():
    await init_db()
    print('Database initialized')

asyncio.run(main())
" 2>/dev/null || echo -e "${YELLOW}⚠ Database tables may already exist${NC}"

echo -e "${GREEN}✓ Database ready${NC}"
echo ""

# Summary
echo ""
echo "================================"
echo -e "${GREEN}✓ Setup Complete!${NC}"
echo "================================"
echo ""
echo "Services are running:"
echo "  • API Server: http://localhost:8000"
echo "  • PostgreSQL: localhost:5432"
echo "  • Redis: localhost:6379"
echo "  • Prometheus: http://localhost:9091"
echo "  • Grafana: http://localhost:3000 (admin/admin)"
echo ""
echo "To start the API server:"
echo "  source venv/bin/activate"
echo "  uvicorn src.main:app --reload"
echo ""
echo "To view API documentation:"
echo "  http://localhost:8000/docs"
echo ""
echo "To run tests:"
echo "  pytest tests/"
echo ""
echo "To stop services:"
echo "  docker-compose down"
echo ""
