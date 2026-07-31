FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# تثبيت المتطلبات أولاً للاستفادة من كاش الطبقات
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ كود المشروع
COPY . .

# مجلدات التحميلات وقاعدة البيانات
RUN mkdir -p /app/downloads /app/data

CMD ["python", "main.py"]
