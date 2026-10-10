# Employee Attendance & Analytics API

A FastAPI backend backed by MongoDB for employee records, punch-in/punch-out, attendance regularization, and attendance analytics.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (MongoDB Atlas is supported)

## Setup

Create and activate a virtual environment, then install dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a local `.env` file with your own MongoDB connection settings:

```dotenv
MONGO_URI=your_mongodb_connection_uri
MONGO_DB=your_database_name
```

Do not commit `.env` or credentials. Real environment variables should take priority over values in `.env`.

## Run

From the repository root:

```powershell
uvicorn app.main:app --port 8000
```

- Health check: `GET /health`
- Interactive API docs: `http://127.0.0.1:8000/docs`

The application creates its MongoDB indexes at startup. No manual index creation should be required.
