# Employee Attendance & Analytics API

A high-performance FastAPI and MongoDB-backed backend service implementing strict employee attendance tracking, manual regularization with audit trails, and multi-dimensional departmental analytics.

Built for the HROne Software Engineer Trainee assignment adhering to the OpenAPI 3.0 contract and business rules (R1–R10).

---

## Technology Stack
- **Language**: Python 3.11+
- **Framework**: FastAPI (Pydantic v2)
- **Database**: MongoDB 6.0+ / 7.0 via PyMongo
- **Server**: Uvicorn ASGI
- **Configuration**: `python-dotenv`

---

## Prerequisites
- Python 3.11 or higher
- MongoDB 6.0+ instance (Local MongoDB or Atlas cluster)

---

## Installation & Setup

1. **Clone the repository and navigate into the workspace:**
   ```bash
   cd candidate_kit
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows (PowerShell):
   .\.venv\Scripts\Activate.ps1
   # Linux/macOS:
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables:**
   Copy `.env.example` to `.env` and configure your MongoDB connection:
   ```bash
   cp .env.example .env
   ```
   Example `.env`:
   ```ini
   MONGO_URI=mongodb://localhost:27017/
   MONGO_DB=hr_attendance
   ```
   *(Note: System environment variables always take precedence over values in `.env`)*

5. **Seed sample data (Optional):**
   ```bash
   python sample_seed.py
   ```

---

## Running the Application

Start the API server:
```bash
uvicorn app.main:app --port 8000
```
*(Use `--reload` during local development)*

- **Interactive API Documentation (Swagger UI)**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc Documentation**: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)
- **Readiness Probe**: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

---

## Implemented Endpoints Summary

### System
- `GET /health` — Readiness probe verifying MongoDB connectivity via database ping (returns 503 if unavailable).

### Employees
- `POST /employees` — Create an employee with unique business `emp_code`, schedule shifts, and department.
- `GET /employees` — Paginated listing sorted by `emp_code` ascending with optional department filtering.

### Attendance
- `POST /attendance/punch-in` — Record daily punch-in; enforces IST date mapping (R1), 10-minute grace period late calculation (R2), and atomic concurrency protection against duplicate punch-ins.
- `POST /attendance/punch-out` — Closes the open attendance record; calculates work hours half-up (R4), overtime (R3), and half-day status (R5), correctly handling overnight shifts across midnight.
- `GET /attendance` — Paginated attendance logs sorted by `(date DESC, emp_code ASC)` with date range and status filters.
- `PATCH /attendance/{emp_code}/{date}` — Manual regularization updating punch records and recomputing derived metrics while appending atomic audit entries into `history`.

### Analytics (MongoDB Aggregation Pipelines)
- `GET /analytics/employees/{emp_code}/monthly` — Monthly summary for an employee (working days, present days, leaves, late occurrences, attendance percentage).
- `GET /analytics/departments/summary` — Department-wide monthly attendance summary preserving zero-log active headcount.
- `GET /analytics/leaderboard/late` — Top late-comers ranked via standard competition ranking (1, 2, 2, 4) with tie-expansion beyond limit.
- `GET /analytics/departments/{department}/trend` — Daily attendance trend with gap filling (`$densify` / continuous date range) and 7-day rolling moving average.

### Administration
- `GET /admin/explain/{endpoint}` — Live MongoDB query execution plans (`executionStats`) for core queries confirming index usage (`IXSCAN`).

---

## Database Indexes & Design Choices

The application initializes the following indexes idempotently at startup:
1. `employees.emp_code` (Unique): Enforces customer business code uniqueness.
2. `attendance_logs.(emp_code, date)` (Unique Compound): Enforces that exactly one attendance document exists per employee per IST calendar day.
3. `attendance_logs.(date DESC, emp_code ASC)`: Accelerates default attendance listing, sorting, and pagination.
4. `attendance_logs.(emp_code, punch_in)`: Accelerates finding matching open punch-in records during punch-out.
5. `attendance_logs.(department, date)`: Accelerates departmental analytics and trend queries.

---

## Testing & Verification

Run the endpoint smoke test suite:
```bash
# Verify health probe
curl -s http://127.0.0.1:8000/health

# Verify explain plan uses index
curl -s "http://127.0.0.1:8000/admin/explain/attendance_list"
```

---

## Known Limitations
- Attendance analytics rely on single-node or replica-set MongoDB instances supporting modern aggregation operators (`$lookup`, `$densify`). Sharded clusters require targeting queries by shard key.
