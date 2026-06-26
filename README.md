# Kia CPO Carnival Scraper

Kia 인증중고차 필터 URL에서 카니발 7인승 선루프 매물을 수집합니다.

수집 필드:

- 가격, 연식/등록월, 주행거리, 차량번호
- 트림 구분: 노블레스 / 시그니처 / 시그니처 그래비티
- 연료 구분: 가솔린 / 하이브리드
- 상세 페이지의 선택옵션 패키지와 각 패키지 세부 옵션
- 옵션 가격, 전체 옵션 수, 대표 옵션, 기본 옵션 카테고리
- 선택옵션 패키지 단위 행과 선택옵션 세부항목 단위 행

## Run

```bash
python3 scrape_kia_cpo.py --verify-dom 3
```

CSV DB까지 갱신하려면:

```bash
python3 scrape_kia_cpo.py \
  --update-db db/kia_cpo_inventory.csv \
  --db-report db/kia_cpo_inventory.update.json
```

기본 URL은 현재 요청한 필터 URL입니다. 다른 필터 URL은 `--target-url`로 넘길 수 있습니다.

```bash
python3 scrape_kia_cpo.py --target-url 'https://cpo.kia.com/products/?filter=...' --verify-dom 3
```

## Outputs

기본 출력 위치는 `data/`입니다.

- `kia_cpo_carnival_7seat_sunroof.json`: 전체 원본 포함 JSON
- `kia_cpo_carnival_7seat_sunroof.csv`: 엑셀용 CSV
- `kia_cpo_carnival_7seat_sunroof.selectable_packages.csv`: 차량별 선택옵션 패키지 1행씩
- `kia_cpo_carnival_7seat_sunroof.selectable_details.csv`: 선택옵션 패키지 내부 세부항목 1행씩
- `kia_cpo_carnival_7seat_sunroof.summary.json`: 건수/트림/연료 요약
- `kia_cpo_carnival_7seat_sunroof.verify.json`: Playwright DOM 검증 결과

`--verify-dom N`은 `.env`의 `PLEOS_ID`, `PLEOS_PW`로 로그인한 뒤, 앞에서부터 N개 상세 페이지를 실제 브라우저로 열어 가격, 등록월, 주행거리, 차량번호, 트림, 연료, 선택옵션명이 화면과 일치하는지 확인합니다.

참고: Kia API는 개별 선택옵션 패키지별 가격을 별도 필드로 제공하지 않습니다. 수집 가능한 가격 정보는 차량 전체 선택옵션 가격(`option_price_won`)이며, 패키지별 CSV에는 이 값을 함께 반복 저장합니다.

## GitHub Actions

`.github/workflows/scrape.yml`은 매시간 기본 API 수집을 실행하고 결과 CSV/JSON을 artifact로 업로드합니다.

또한 `db/kia_cpo_inventory.csv`를 차량번호(`plate_number`) 기준 CSV DB로 갱신하고 커밋합니다.

- 새로 발견된 차량: `status=available`로 추가
- 계속 보이는 차량: `last_seen_at`, `last_scraped_at`, `seen_count` 갱신
- 이전 DB에는 있었지만 이번 수집에서 사라진 차량: 행을 유지하고 `status=sold_out`, `sold_out_at` 설정
- sold out 차량이 다시 보이면 `status=available`로 되돌리고 `sold_out_at` 비움

수동 실행에서 DOM 검증까지 켜려면 repository secrets에 아래 값을 등록한 뒤 `verify_dom` 입력을 `3`처럼 0보다 크게 설정합니다.

- `PLEOS_ID`
- `PLEOS_PW`

기본 스케줄 실행은 로그인 없이 공개 API만 사용하므로 secrets가 없어도 동작합니다.

새 차량 알림을 webhook으로 받고 싶으면 repository secret에 `NOTIFY_WEBHOOK_URL`을 등록합니다. 새 차량이 있을 때만 아래 형태의 JSON이 POST됩니다.

```json
{
  "event": "kia_cpo_new_vehicles",
  "added_count": 1,
  "vehicles": [
    {
      "plate_number": "123가4567",
      "detail_url": "https://cpo.kia.com/products/detail/?id=..."
    }
  ]
}
```

iMessage로 직접 받으려면 GitHub-hosted Actions가 아니라, 이 webhook을 받아 Apple Messages 앱으로 전송하는 Mac 브릿지 또는 macOS self-hosted runner가 필요합니다.
