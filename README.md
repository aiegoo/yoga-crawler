# yoga-crawler

Automated crawler for Korean yoga resources — studios, instructors, and associations — using the Kakao Local API and Naver Search API. Data lands in S3 (`yogaq-crawl-raw-ap2`) and optionally seeds a PostgreSQL database.

## Structure

```
yoga-crawler/
├── scripts/
│   ├── scrape_studios.py        # Kakao + Naver studio crawler (25 cities)
│   ├── scrape_instructors.py    # Yoga Alliance + Instagram instructor crawler
│   └── scrape_associations.py   # Korean yoga association/alliance crawler
├── pipeline.sh                  # Daily orchestration script (runs via cron)
├── requirements.txt
├── .env.example                 # Required environment variables
└── data/                        # Local output (gitignored, also synced to S3)
```

## Setup (EC2 / Ubuntu)

```bash
# 1. Clone
git clone https://github.com/aiegoo/yoga-crawler.git
cd yoga-crawler

# 2. Python venv
python3 -m venv ~/venv
source ~/venv/bin/activate
pip install -r requirements.txt

# 3. Environment variables
cp .env.example .env
# edit .env with your keys, then load:
sudo sh -c 'cat .env >> /etc/environment'

# 4. Make pipeline executable
chmod +x pipeline.sh

# 5. Cron (03:00 KST daily)
(crontab -l; echo "0 18 * * * /home/ubuntu/yoga-crawler/pipeline.sh >> /home/ubuntu/yoga-crawler/logs/cron.log 2>&1") | crontab -
```

## Usage

```bash
# Dry-run (no API calls)
./pipeline.sh --dry-run

# Studios only (all 25 cities)
./pipeline.sh --only studios

# Full run
./pipeline.sh

# Direct script usage
source ~/venv/bin/activate
python scripts/scrape_studios.py --all-cities --s3-sync --delay 0.5
python scripts/scrape_studios.py --cities 서울 부산 --dry-run
python scripts/scrape_associations.py --source all --s3-sync
```

## Data Sources

| Source | Coverage | API |
|--------|----------|-----|
| Kakao Local | 25 Korean cities × 9 keywords | REST API Key |
| Naver Search | 25 Korean cities × 9 keywords | Client ID + Secret |
| Yoga Alliance | KR-filtered directory | HTTP scrape |
| 대한요가회 | koreayoga.or.kr | HTTP scrape |
| Instagram | Yoga hashtags | instaloader |

## S3 Output

```
s3://yogaq-crawl-raw-ap2/
└── YYYY-MM-DD/
    ├── studios/
    │   ├── studios_raw.json
    │   └── studios_seed.sql
    ├── instructors/
    └── associations/
```

## Cost Estimate

~11,930 KRW/month (t3.micro always-on + S3 + local PostgreSQL).  
Approval gate: >90,000 KRW requires sign-off.

## Authors

- [@aiegoo](https://github.com/aiegoo)
