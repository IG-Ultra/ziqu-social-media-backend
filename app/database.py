"""
Database Engine & Connection Pool Configuration
PostgreSQL Connection with SQLAlchemy 2.0
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://postgres:password@localhost:5432/ziqu_db"
)

# SQLAlchemy Connection Pool configuration for high concurrency
engine = create_engine(
    DATABASE_URL,
    pool_size=20,            # 20 active connections per worker
    max_overflow=40,         # Burst allowance
    pool_timeout=30,
    pool_recycle=1800,       # Recycle connections every 30 mins
    echo=False               # Set True to log raw SQL statements
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI Dependency for database session per request."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
