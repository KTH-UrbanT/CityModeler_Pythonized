FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg

WORKDIR /app

# Fiona may need compilation on ARM64; GDAL supplies its native GIS library.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY Geometry/requirements.txt Geometry/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY Features/Scripts/ Scripts/
COPY Features/Configs/ Configs/
COPY Features/main_Data4ExternalStudy.py Features/main_Generate_CityModeller.py ./

CMD ["python", "main_Data4ExternalStudy.py"]
