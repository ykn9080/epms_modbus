FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

#COPY register_map.py simulator.py collector.py point_map.yaml ./
COPY . .
ENV MODBUS_PORT=502
CMD ["python", "-u", "simulator.py"]
