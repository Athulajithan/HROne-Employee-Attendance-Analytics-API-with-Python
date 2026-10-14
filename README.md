# HROne Attendance & Analytics API

A backend API built with **FastAPI and MongoDB** to manage employee attendance, handle attendance corrections, and generate useful attendance analytics.

The project covers the core attendance workflow, from employee registration and daily punch-in/punch-out to monthly summaries and department-level reporting.

## What this project does

- **Employee management:** Create and retrieve employee records with validation and pagination.
- **Attendance tracking:** Record employee punch-ins and punch-outs while applying the required attendance rules.
- **Attendance correction:** Update attendance records when corrections are needed and maintain an audit history.
- **Monthly analytics:** View attendance summaries for individual employees.
- **Department analytics:** Generate department summaries, attendance trends, and late-arrival leaderboards.
- **Query inspection:** Inspect MongoDB query plans through an explain endpoint.
- **Health monitoring:** Check the API and MongoDB connection status.

## Tech stack

- Python 3.11+
- FastAPI
- PyMongo
- Pydantic v2
- MongoDB 6+

## Getting started

### 1. Clone the repository

```powershell
git clone <your-public-repository-url>
cd <your-repository-folder>
```

Replace the placeholders with your actual GitHub repository URL and folder name.

### 2. Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 4. Configure MongoDB

Create a local `.env` file in the project root:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

You can use a MongoDB Atlas connection URI instead if you are using a cloud database.

Keep your `.env` file private and never commit database credentials to GitHub.

### 5. Load sample data (optional)

If you want to test the API with sample records, run:

```powershell
python sample_seed.py
```

Make sure your MongoDB connection is configured before running the seed script.

## Run the API

Start the development server from the project root:

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

Once the server is running, you can access:

- **Swagger UI:** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **OpenAPI schema:** [http://127.0.0.1:8000/openapi.json](http://127.0.0.1:8000/openapi.json)

Swagger UI lets you explore the available endpoints and send requests directly from your browser.

## Run the tests

Run the test suite from the project root:

```powershell
python -m pytest -q
```

The command runs the available automated tests. Make sure the required dependencies are installed and any necessary test database configuration is available.

## API functionality

The API includes endpoints for:

| Area | Functionality |
|---|---|
| Health | Check API and MongoDB availability |
| Employees | Create and list employee records |
| Attendance | Punch-in, punch-out, and retrieve attendance records |
| Regularization | Correct attendance records with audit history |
| Employee analytics | Generate monthly attendance summaries |
| Department analytics | Summarize department attendance and trends |
| Late leaderboard | Report late arrivals and rankings |
| Query explain | Inspect MongoDB query execution plans |

The API follows the request and response contracts defined in `openapi.yaml` and the data model described in `DATA_MODEL.md`.

## Data and design considerations

Attendance dates follow the project's IST-based rules, while timestamps use epoch milliseconds as specified by the API contract.

MongoDB indexes support employee-code uniqueness, attendance-day uniqueness, and attendance queries. Attendance corrections retain an audit history so that changes can be reviewed.

## Project structure

```text
.
├── app/
│   └── main.py
├── requirements.txt
├── openapi.yaml
├── DATA_MODEL.md
├── README.md
├── REVIEW.md
├── DECISIONS.md
└── sample_seed.py
```

This is the main project layout; additional test files and configuration files may also be present.

## Security and limitations

- Database credentials should be stored in a local `.env` file and excluded from version control.
- The project focuses on backend API functionality and does not include a frontend.
- A Dockerfile and deployment configuration are not included.

For the exact endpoint contracts and data requirements, refer to `openapi.yaml` and `DATA_MODEL.md`.

