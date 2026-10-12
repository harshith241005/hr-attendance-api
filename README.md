# Employee Attendance & Analytics API

A FastAPI and MongoDB service for employee attendance tracking, manual corrections with audit history, and reporting analytics powered by aggregation pipelines.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (local instance, Docker, or MongoDB Atlas)

## Setup & Running

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment variables:**
   Set `MONGO_URI` and `MONGO_DB` (or define in a local `.env`):
   ```bash
   MONGO_URI="mongodb://localhost:27017"
   MONGO_DB="attendance_db"
   ```

3. **Start the application:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   All required indexes on `employees` and `attendance_logs` are initialized automatically and idempotently during startup.

## Documentation & Decisions
- **`REVIEW.md`**: Detailed analysis and resolution of starter defects (Part A).
- **`DECISIONS.md`**: Architectural decisions regarding indexes, concurrency handling, tie-breaking, headcount calculation, and scale (Part C).
- **`app/main.py`**: Complete application implementation containing all CRUD endpoints, business rules (R1–R10), and aggregation pipelines.
