FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ffmpeg لازم لدمج سجمنتات DASH (remux) في مسار التحميل الاحتياطي
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

# تثبيت المتطلبات أولاً للاستفادة من كاش الطبقات
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ كود المشروع
COPY . .

# مجلدات التحميلات وقاعدة البيانات
RUN mkdir -p /app/downloads /app/data

CMD ["python", "main.py"]
