import os
import asyncio
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Text
import bcrypt

# SQLite Async Engine for custom tables
db_path = "app.db"
engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
AsyncSessionLocal = sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)
Base = declarative_base()

def verify_password(plain_password, hashed_password):
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
    except ValueError:
        return False

def get_password_hash(password):
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

class AppUser(Base):
    __tablename__ = "app_users"
    id = Column(String, primary_key=True) # Chainlit user identifier (email)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    avatar_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Meeting(Base):
    __tablename__ = "meetings"
    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    thread_id = Column(String, nullable=True)
    filename = Column(String, nullable=False)
    transcript = Column(Text, nullable=False)
    mom_summary = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# Migrate existing meetings to default user if needed
async def migrate_existing_meetings():
    async with AsyncSessionLocal() as session:
        pass # Optional logic
