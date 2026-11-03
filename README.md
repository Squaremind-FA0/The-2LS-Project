# Full-Stack (React + FastAPI)

Backend:
```bash
cd backend && pip install -r requirements.txt && uvicorn main:app --reload
```
Frontend:
```bash
cd frontend && npm install && npm run dev
```
Single-origin (docker / preview):
```bash
docker build -t app . && docker run -p 8080:8080 app
```
