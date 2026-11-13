.PHONY: install backend frontend regression

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 告警聚合与恢复回归（ALER-0002 × TASK-0002），TestClient 需要 httpx
regression:
	cd backend && .venv/bin/pip install -q httpx && .venv/bin/python tests/regression_alert.py
