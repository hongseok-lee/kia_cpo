#!/usr/bin/env python3
"""Scrape Kia CPO filtered listings and selected option packages.

The scraper uses Kia CPO's JSON endpoints for accuracy and speed, then can
optionally verify sampled rows against the rendered detail pages with
Playwright. Credentials are only needed for DOM verification.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_TARGET_URL = (
    "https://cpo.kia.com/products/?filter=size%3D10%26reserved%3D%26sort%3D"
    "DISPLAYED_AT_DESC%26displayChannel%3DGENERAL%26categoryAndModelCodeName"
    "%255B%255D%3DSUV%2520%25EC%25B9%25B4%25EB%258B%2588%25EB%25B0"
    "%259C%2520%25ED%2595%2598%25EC%259D%25B4%25EB%25A6%25AC%25EB"
    "%25AC%25B4%25EC%25A7%2584%26categoryAndModelCodeName%255B%255D%3D"
    "SUV%2520%25EC%25B9%25B4%25EB%258B%2588%25EB%25B0%259C%2520"
    "%25EC%2595%2584%25EC%259B%2583%25EB%258F%2584%25EC%2596%25B4"
    "%26categoryAndModelCodeName%255B%255D%3DSUV%2520%25EC%25B9%25B4"
    "%25EB%258B%2588%25EB%25B0%259C%26modelDoors%255B%255D%3D7"
    "%25EC%259D%25B8%25EC%258A%25B9%26options%255B%255D%3DSUNROOF"
    "%26fuels%255B%255D%3DGASOLINE%26fuels%255B%255D%3DHYBRID"
)

BASE_URL = "https://cpo.kia.com"
API_BASE = f"{BASE_URL}/api"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


class ScrapeError(RuntimeError):
    """Raised when data cannot be fetched or validated."""


@dataclass(frozen=True)
class OutputPaths:
    json_path: Path
    csv_path: Path
    selectable_packages_csv_path: Path
    selectable_details_csv_path: Path
    summary_path: Path
    verify_path: Path


MAIN_OPTION_LABELS = {
    "LEATHER_SEATS": "가죽시트",
    "NAVIGATION": "내비게이션",
    "WIRELESS_CHARGING": "무선충전",
    "AUTOMATIC_TRUNK": "전동트렁크",
    "SUNROOF": "썬루프",
    "SMART_KEY": "스마트키",
    "HEATED_SEATS": "운전석 열선시트",
    "PARKING_DISTANCE_WARNING": "후방 주차거리경고",
    "ADAS": "첨단 운전자 보조 (ADAS)",
    "VENTILATED_SEATS": "운전석 통풍시트",
    "HIPASS": "하이패스",
    "HUD": "헤드업 디스플레이",
    "REAR_VIEW_CAMERA": "후방카메라",
}


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def parse_filter_params(target_url: str) -> list[tuple[str, str]]:
    parsed = urlparse(target_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    filter_query = query.get("filter")
    if not filter_query:
        raise ScrapeError("target URL must contain a filter= query parameter")
    return parse_qsl(filter_query, keep_blank_values=True)


def params_with_size(params: list[tuple[str, str]], size: int) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    saw_size = False
    for key, value in params:
        if key == "size":
            if not saw_size:
                result.append((key, str(size)))
                saw_size = True
            continue
        result.append((key, value))
    if not saw_size:
        result.insert(0, ("size", str(size)))
    return result


def build_search_url(params: list[tuple[str, str]], size: int) -> str:
    return f"{API_BASE}/search/?{urlencode(params_with_size(params, size), doseq=True)}"


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def compact_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [normalize_space(v) for v in values if normalize_space(v)]


def normalize_main_options(values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(values, list):
        return result
    for order, item in enumerate(values, start=1):
        if not isinstance(item, dict):
            code = normalize_space(item)
            result.append(
                {
                    "order": order,
                    "code": code,
                    "name": MAIN_OPTION_LABELS.get(code, code),
                    "has": True,
                }
            )
            continue
        code = normalize_space(item.get("mainOption"))
        result.append(
            {
                "order": order,
                "code": code,
                "name": MAIN_OPTION_LABELS.get(code, code),
                "has": bool(item.get("has")),
            }
        )
    return result


def month_label(date_value: str | None) -> str:
    if not date_value:
        return ""
    match = re.match(r"^(\d{4})-(\d{2})", date_value)
    if not match:
        return date_value
    return f"{match.group(1)}년 {match.group(2)}월"


def fuel_group(engine: str, fuel_type: str | None) -> str:
    engine_text = normalize_space(engine)
    api_fuel = normalize_space(fuel_type).upper()
    if api_fuel == "HYBRID" or "하이브리드" in engine_text or "HEV" in engine_text.upper():
        return "HYBRID"
    if api_fuel == "GASOLINE" or "가솔린" in engine_text:
        return "GASOLINE"
    return api_fuel or engine_text


def fuel_label(group: str) -> str:
    return {"HYBRID": "하이브리드", "GASOLINE": "가솔린"}.get(group, group)


def trim_group(trim: str, model_name: str) -> str:
    text = f"{normalize_space(trim)} {normalize_space(model_name)}"
    if "그래비티" in text:
        return "시그니처 그래비티"
    if "시그니처" in text:
        return "시그니처"
    if "노블레스" in text:
        return "노블레스"
    return normalize_space(trim) or "미분류"


def format_won(value: int | None) -> str:
    if value is None:
        return ""
    return f"{value:,}원"


def format_manwon(value: int | None) -> str:
    if value is None:
        return ""
    return f"{value // 10000:,}만원"


def format_km(value: int | None) -> str:
    if value is None:
        return ""
    return f"{value:,}km"


class KiaApiClient:
    def __init__(self, retries: int = 3, timeout: int = 30, delay: float = 0.4) -> None:
        self.retries = retries
        self.timeout = timeout
        self.delay = delay

    def get_json(self, url: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                request = Request(
                    url,
                    headers={
                        "accept": "application/json",
                        "user-agent": USER_AGENT,
                        "referer": BASE_URL,
                    },
                )
                with urlopen(request, timeout=self.timeout) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return json.loads(response.read().decode(charset))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(min(4.0, self.delay * (2 ** (attempt - 1))))
        raise ScrapeError(f"GET failed after {self.retries} attempts: {url}: {last_error}")


def fetch_listing_rows(client: KiaApiClient, params: list[tuple[str, str]], limit: int | None) -> list[dict[str, Any]]:
    first = client.get_json(build_search_url(params, 1))
    total = int(first.get("totalElements") or 0)
    if total <= 0:
        return []
    size = min(limit or total, total)
    data = client.get_json(build_search_url(params, size))
    rows = data.get("content") or []
    if len(rows) < size:
        raise ScrapeError(f"expected {size} listing rows, got {len(rows)}")
    return rows[:size]


def fetch_vehicle(client: KiaApiClient, listing: dict[str, Any]) -> dict[str, Any]:
    product_id = listing.get("id")
    if product_id is None:
        raise ScrapeError(f"listing without id: {listing}")
    detail = client.get_json(f"{API_BASE}/product/detail/{product_id}/")
    options = client.get_json(f"{API_BASE}/product/options/{product_id}/")
    return normalize_vehicle(listing, detail, options)


def normalize_vehicle(
    listing: dict[str, Any],
    detail: dict[str, Any],
    options: dict[str, Any],
) -> dict[str, Any]:
    product_id = int(detail.get("id") or listing["id"])
    car = detail.get("car") or {}
    price = car.get("price", listing.get("price"))
    mileage = car.get("drivingDistance", listing.get("drivingDistance"))
    registered = car.get("firstRegisteredOn", listing.get("firstRegisteredOn"))
    engine = car.get("engine", listing.get("modelEngine"))
    trim = car.get("trim", listing.get("modelTrim"))
    model_name = car.get("modelName", listing.get("modelName"))
    fuel = fuel_group(engine, car.get("fuelType"))

    selectable = []
    for order, item in enumerate(options.get("selectable") or [], start=1):
        details = compact_list(item.get("details"))
        selectable.append(
            {
                "order": order,
                "name": normalize_space(item.get("name")),
                "details": details,
                "detail_count": len(details),
                "has_details": bool(details),
                "details_text": "; ".join(details),
            }
        )

    basic_options = {
        key: compact_list(options.get(key))
        for key in ["ptpe", "safety", "exterior", "interior", "seat", "comport", "multimedia"]
    }
    consistency = {
        "price": listing.get("price") == price,
        "drivingDistance": listing.get("drivingDistance") == mileage,
        "firstRegisteredOn": listing.get("firstRegisteredOn") == registered,
        "modelTrim": normalize_space(listing.get("modelTrim")) == normalize_space(trim),
        "modelEngine": normalize_space(listing.get("modelEngine")) == normalize_space(engine),
    }

    main_options = normalize_main_options(detail.get("mainOptions"))
    selected_main_options = [item for item in main_options if item["has"]]

    result = {
        "id": product_id,
        "detail_url": f"{BASE_URL}/products/detail/?id={product_id}",
        "price_won": price,
        "price_text": format_manwon(price),
        "model_year": car.get("modelYear", listing.get("modelYear")),
        "first_registered_on": registered,
        "first_registered_month": month_label(registered),
        "mileage_km": mileage,
        "mileage_text": format_km(mileage),
        "plate_number": car.get("plateNumber", listing.get("plateNumber")),
        "model_name": normalize_space(model_name),
        "model_code_name": car.get("modelCodeName", listing.get("modelCodeName")),
        "door": car.get("door", listing.get("modelDoor")),
        "engine": normalize_space(engine),
        "fuel_type": fuel,
        "fuel_label": fuel_label(fuel),
        "mission": car.get("mission", listing.get("modelMission")),
        "trim_raw": normalize_space(trim),
        "trim_group": trim_group(trim, model_name),
        "classification": detail.get("classification", listing.get("classification")),
        "reserved": detail.get("reservation", listing.get("reserved")),
        "wish_count": listing.get("wishCount"),
        "option_price_won": detail.get("optionPrice"),
        "option_price_text": format_manwon(detail.get("optionPrice")),
        "option_count": detail.get("optionCount", options.get("optionCount")),
        "main_options": main_options,
        "main_option_names": [item["name"] for item in selected_main_options],
        "main_option_codes": [item["code"] for item in selected_main_options],
        "main_option_count": len(selected_main_options),
        "selectable_options": selectable,
        "selectable_option_names": [item["name"] for item in selectable],
        "selectable_option_package_count": len(selectable),
        "selectable_option_detail_total": sum(item["detail_count"] for item in selectable),
        "selectable_option_details_flat": [
            {
                "package_order": package["order"],
                "package_name": package["name"],
                "detail_order": detail_order,
                "detail": detail,
            }
            for package in selectable
            for detail_order, detail in enumerate(package["details"], start=1)
        ],
        "basic_options": basic_options,
        "basic_option_counts": {key: len(value) for key, value in basic_options.items()},
        "source_consistency": consistency,
        "raw": {"listing": listing, "detail": detail, "options": options},
    }
    validate_vehicle(result)
    return result


def validate_vehicle(vehicle: dict[str, Any]) -> None:
    required = ["id", "price_won", "first_registered_month", "mileage_km", "plate_number", "trim_group", "fuel_type"]
    missing = [key for key in required if vehicle.get(key) in (None, "")]
    if missing:
        raise ScrapeError(f"vehicle {vehicle.get('id')} missing required fields: {', '.join(missing)}")
    if vehicle["fuel_type"] not in {"GASOLINE", "HYBRID"}:
        raise ScrapeError(f"vehicle {vehicle['id']} has unexpected fuel: {vehicle['fuel_type']}")
    if vehicle["trim_group"] not in {"노블레스", "시그니처", "시그니처 그래비티"}:
        # Keep the row, but make drift visible in the summary.
        vehicle["trim_group_warning"] = True
    if not all(vehicle["source_consistency"].values()):
        vehicle["source_consistency_warning"] = True


def summarize(vehicles: list[dict[str, Any]]) -> dict[str, Any]:
    by_trim: dict[str, int] = {}
    by_fuel: dict[str, int] = {}
    warnings: list[dict[str, Any]] = []
    for vehicle in vehicles:
        by_trim[vehicle["trim_group"]] = by_trim.get(vehicle["trim_group"], 0) + 1
        by_fuel[vehicle["fuel_type"]] = by_fuel.get(vehicle["fuel_type"], 0) + 1
        if vehicle.get("trim_group_warning") or vehicle.get("source_consistency_warning"):
            warnings.append(
                {
                    "id": vehicle["id"],
                    "trim_group": vehicle["trim_group"],
                    "source_consistency": vehicle["source_consistency"],
                }
            )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(vehicles),
        "by_trim": by_trim,
        "by_fuel": by_fuel,
        "warnings": warnings,
    }


def output_paths(output_dir: Path, prefix: str) -> OutputPaths:
    return OutputPaths(
        json_path=output_dir / f"{prefix}.json",
        csv_path=output_dir / f"{prefix}.csv",
        selectable_packages_csv_path=output_dir / f"{prefix}.selectable_packages.csv",
        selectable_details_csv_path=output_dir / f"{prefix}.selectable_details.csv",
        summary_path=output_dir / f"{prefix}.summary.json",
        verify_path=output_dir / f"{prefix}.verify.json",
    )


def write_outputs(vehicles: list[dict[str, Any]], paths: OutputPaths, target_url: str) -> None:
    paths.json_path.parent.mkdir(parents=True, exist_ok=True)
    summary = summarize(vehicles)
    payload = {
        "target_url": target_url,
        "summary": summary,
        "vehicles": vehicles,
    }
    paths.json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    paths.summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_fields = [
        "id",
        "detail_url",
        "price_won",
        "price_text",
        "model_year",
        "first_registered_on",
        "first_registered_month",
        "mileage_km",
        "mileage_text",
        "plate_number",
        "model_name",
        "engine",
        "fuel_type",
        "fuel_label",
        "trim_raw",
        "trim_group",
        "classification",
        "option_price_won",
        "option_price_text",
        "option_count",
        "main_option_count",
        "main_options",
        "main_option_codes",
        "selectable_option_names",
        "selectable_option_package_count",
        "selectable_option_detail_total",
        "selectable_option_details",
        "basic_option_counts_json",
        "selectable_options_json",
    ]
    with paths.csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=csv_fields)
        writer.writeheader()
        for vehicle in vehicles:
            row = {key: vehicle.get(key) for key in csv_fields}
            row["main_options"] = "; ".join(vehicle["main_option_names"])
            row["main_option_codes"] = "; ".join(vehicle["main_option_codes"])
            row["selectable_option_names"] = "; ".join(vehicle["selectable_option_names"])
            row["selectable_option_details"] = " | ".join(
                f"{item['package_name']} > {item['detail']}" for item in vehicle["selectable_option_details_flat"]
            )
            row["basic_option_counts_json"] = json.dumps(vehicle["basic_option_counts"], ensure_ascii=False)
            row["selectable_options_json"] = json.dumps(vehicle["selectable_options"], ensure_ascii=False)
            writer.writerow(row)

    package_fields = [
        "vehicle_id",
        "detail_url",
        "plate_number",
        "price_won",
        "price_text",
        "model_year",
        "first_registered_on",
        "first_registered_month",
        "mileage_km",
        "mileage_text",
        "fuel_type",
        "fuel_label",
        "trim_raw",
        "trim_group",
        "option_price_won",
        "option_price_text",
        "option_count",
        "package_order",
        "package_name",
        "package_detail_count",
        "package_has_details",
        "package_details",
        "package_details_json",
    ]
    with paths.selectable_packages_csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=package_fields)
        writer.writeheader()
        for vehicle in vehicles:
            for package in vehicle["selectable_options"]:
                writer.writerow(
                    {
                        "vehicle_id": vehicle["id"],
                        "detail_url": vehicle["detail_url"],
                        "plate_number": vehicle["plate_number"],
                        "price_won": vehicle["price_won"],
                        "price_text": vehicle["price_text"],
                        "model_year": vehicle["model_year"],
                        "first_registered_on": vehicle["first_registered_on"],
                        "first_registered_month": vehicle["first_registered_month"],
                        "mileage_km": vehicle["mileage_km"],
                        "mileage_text": vehicle["mileage_text"],
                        "fuel_type": vehicle["fuel_type"],
                        "fuel_label": vehicle["fuel_label"],
                        "trim_raw": vehicle["trim_raw"],
                        "trim_group": vehicle["trim_group"],
                        "option_price_won": vehicle["option_price_won"],
                        "option_price_text": vehicle["option_price_text"],
                        "option_count": vehicle["option_count"],
                        "package_order": package["order"],
                        "package_name": package["name"],
                        "package_detail_count": package["detail_count"],
                        "package_has_details": package["has_details"],
                        "package_details": package["details_text"],
                        "package_details_json": json.dumps(package["details"], ensure_ascii=False),
                    }
                )

    detail_fields = [
        "vehicle_id",
        "detail_url",
        "plate_number",
        "price_text",
        "first_registered_month",
        "mileage_text",
        "fuel_label",
        "trim_group",
        "option_price_text",
        "package_order",
        "package_name",
        "detail_order",
        "detail",
    ]
    with paths.selectable_details_csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=detail_fields)
        writer.writeheader()
        for vehicle in vehicles:
            for detail in vehicle["selectable_option_details_flat"]:
                writer.writerow(
                    {
                        "vehicle_id": vehicle["id"],
                        "detail_url": vehicle["detail_url"],
                        "plate_number": vehicle["plate_number"],
                        "price_text": vehicle["price_text"],
                        "first_registered_month": vehicle["first_registered_month"],
                        "mileage_text": vehicle["mileage_text"],
                        "fuel_label": vehicle["fuel_label"],
                        "trim_group": vehicle["trim_group"],
                        "option_price_text": vehicle["option_price_text"],
                        "package_order": detail["package_order"],
                        "package_name": detail["package_name"],
                        "detail_order": detail["detail_order"],
                        "detail": detail["detail"],
                    }
                )


DB_FIELDS = [
    "plate_number",
    "status",
    "product_id",
    "detail_url",
    "price_won",
    "price_text",
    "model_year",
    "first_registered_on",
    "first_registered_month",
    "mileage_km",
    "mileage_text",
    "model_name",
    "engine",
    "fuel_type",
    "fuel_label",
    "trim_raw",
    "trim_group",
    "classification",
    "option_price_won",
    "option_price_text",
    "option_count",
    "main_option_count",
    "main_options",
    "selectable_option_names",
    "selectable_option_package_count",
    "selectable_option_detail_total",
    "selectable_options_json",
    "first_seen_at",
    "last_seen_at",
    "last_scraped_at",
    "sold_out_at",
    "seen_count",
    "missing_count",
]


def read_db(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))
    db: dict[str, dict[str, str]] = {}
    for row in rows:
        plate_number = normalize_space(row.get("plate_number"))
        if plate_number:
            db[plate_number] = {field: row.get(field, "") for field in DB_FIELDS}
    return db


def int_from_row(row: dict[str, str], key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except ValueError:
        return 0


def vehicle_db_values(vehicle: dict[str, Any]) -> dict[str, str]:
    return {
        "plate_number": str(vehicle["plate_number"]),
        "product_id": str(vehicle["id"]),
        "detail_url": str(vehicle["detail_url"]),
        "price_won": str(vehicle["price_won"]),
        "price_text": str(vehicle["price_text"]),
        "model_year": str(vehicle["model_year"]),
        "first_registered_on": str(vehicle["first_registered_on"]),
        "first_registered_month": str(vehicle["first_registered_month"]),
        "mileage_km": str(vehicle["mileage_km"]),
        "mileage_text": str(vehicle["mileage_text"]),
        "model_name": str(vehicle["model_name"]),
        "engine": str(vehicle["engine"]),
        "fuel_type": str(vehicle["fuel_type"]),
        "fuel_label": str(vehicle["fuel_label"]),
        "trim_raw": str(vehicle["trim_raw"]),
        "trim_group": str(vehicle["trim_group"]),
        "classification": str(vehicle["classification"]),
        "option_price_won": str(vehicle["option_price_won"]),
        "option_price_text": str(vehicle["option_price_text"]),
        "option_count": str(vehicle["option_count"]),
        "main_option_count": str(vehicle["main_option_count"]),
        "main_options": "; ".join(vehicle["main_option_names"]),
        "selectable_option_names": "; ".join(vehicle["selectable_option_names"]),
        "selectable_option_package_count": str(vehicle["selectable_option_package_count"]),
        "selectable_option_detail_total": str(vehicle["selectable_option_detail_total"]),
        "selectable_options_json": json.dumps(vehicle["selectable_options"], ensure_ascii=False),
    }


def update_csv_db(vehicles: list[dict[str, Any]], path: Path, scraped_at: str) -> dict[str, Any]:
    existing = read_db(path)
    current_by_plate = {str(vehicle["plate_number"]): vehicle for vehicle in vehicles}
    rows_by_plate: dict[str, dict[str, str]] = {}
    added: list[str] = []
    reappeared: list[str] = []
    sold_out: list[str] = []

    for plate_number, vehicle in current_by_plate.items():
        old = existing.get(plate_number, {})
        row = {field: old.get(field, "") for field in DB_FIELDS}
        was_sold_out = old.get("status") == "sold_out"
        row.update(vehicle_db_values(vehicle))
        row["status"] = "available"
        row["first_seen_at"] = old.get("first_seen_at") or scraped_at
        row["last_seen_at"] = scraped_at
        row["last_scraped_at"] = scraped_at
        row["sold_out_at"] = ""
        row["seen_count"] = str(int_from_row(old, "seen_count") + 1)
        row["missing_count"] = "0"
        rows_by_plate[plate_number] = row
        if not old:
            added.append(plate_number)
        elif was_sold_out:
            reappeared.append(plate_number)

    for plate_number, old in existing.items():
        if plate_number in current_by_plate:
            continue
        row = {field: old.get(field, "") for field in DB_FIELDS}
        row["status"] = "sold_out"
        row["last_scraped_at"] = scraped_at
        row["sold_out_at"] = old.get("sold_out_at") or scraped_at
        row["missing_count"] = str(int_from_row(old, "missing_count") + 1)
        row["seen_count"] = str(int_from_row(old, "seen_count"))
        rows_by_plate[plate_number] = row
        if old.get("status") != "sold_out":
            sold_out.append(plate_number)

    ordered_rows = sorted(
        rows_by_plate.values(),
        key=lambda row: (
            0 if row["status"] == "available" else 1,
            row.get("last_seen_at", ""),
            row["plate_number"],
        ),
        reverse=False,
    )
    # Keep available rows in the current site order for easier visual comparison.
    site_order = {plate: index for index, plate in enumerate(current_by_plate)}
    ordered_rows.sort(
        key=lambda row: (
            0 if row["status"] == "available" else 1,
            site_order.get(row["plate_number"], 10_000),
            row["plate_number"],
        )
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=DB_FIELDS)
        writer.writeheader()
        writer.writerows(ordered_rows)

    available_count = sum(1 for row in ordered_rows if row["status"] == "available")
    sold_out_count = sum(1 for row in ordered_rows if row["status"] == "sold_out")
    return {
        "path": str(path),
        "scraped_at": scraped_at,
        "total_rows": len(ordered_rows),
        "available_count": available_count,
        "sold_out_count": sold_out_count,
        "added": added,
        "added_vehicles": [
            {
                "plate_number": plate,
                "product_id": current_by_plate[plate]["id"],
                "detail_url": current_by_plate[plate]["detail_url"],
                "price_text": current_by_plate[plate]["price_text"],
                "first_registered_month": current_by_plate[plate]["first_registered_month"],
                "mileage_text": current_by_plate[plate]["mileage_text"],
                "fuel_label": current_by_plate[plate]["fuel_label"],
                "trim_group": current_by_plate[plate]["trim_group"],
                "selectable_option_names": current_by_plate[plate]["selectable_option_names"],
            }
            for plate in added
        ],
        "sold_out": sold_out,
        "sold_out_vehicles": [
            {
                "plate_number": plate,
                "product_id": rows_by_plate[plate].get("product_id"),
                "detail_url": rows_by_plate[plate].get("detail_url"),
                "price_text": rows_by_plate[plate].get("price_text"),
                "first_registered_month": rows_by_plate[plate].get("first_registered_month"),
                "mileage_text": rows_by_plate[plate].get("mileage_text"),
                "fuel_label": rows_by_plate[plate].get("fuel_label"),
                "trim_group": rows_by_plate[plate].get("trim_group"),
            }
            for plate in sold_out
        ],
        "reappeared": reappeared,
    }


def verify_with_playwright(
    vehicles: list[dict[str, Any]],
    sample_size: int,
    env_path: Path,
    output_path: Path,
    headless: bool,
) -> dict[str, Any]:
    if sample_size <= 0:
        return {"enabled": False}
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - depends on local environment.
        raise ScrapeError(f"Playwright is required for DOM verification: {exc}") from exc

    env = load_env(env_path)
    user = env.get("PLEOS_ID")
    password = env.get("PLEOS_PW")
    if not user or not password:
        raise ScrapeError(f"{env_path} must contain PLEOS_ID and PLEOS_PW for DOM verification")

    def login(page: Any) -> None:
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90_000)
        try:
            page.get_by_role("button", name="닫기").click(timeout=5_000)
        except PlaywrightTimeoutError:
            pass
        page.get_by_role("button", name="로그인").click(timeout=30_000)
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        page.get_by_role("link", name=re.compile("Pleos.*로그인")).click(timeout=30_000)
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        page.locator('input[name="email"]').fill(user, timeout=30_000)
        page.locator('input[name="password"]').fill(password, timeout=30_000)
        page.get_by_role("button", name="로그인").click(timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=45_000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(3_000)

    checked: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(locale="ko-KR", viewport={"width": 1440, "height": 1400})
        page = context.new_page()
        login(page)
        for vehicle in vehicles[:sample_size]:
            page.goto(vehicle["detail_url"], wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(5_000)
            try:
                option_button = page.locator("button.expand-btn.js-expand-btn").filter(
                    has_text=re.compile("옵션 자세히 보기")
                ).first
                option_button.scroll_into_view_if_needed(timeout=10_000)
                option_button.click(timeout=10_000)
                page.wait_for_timeout(1_000)
            except Exception:
                pass
            body = normalize_space(page.locator("body").inner_text(timeout=15_000))
            checks = {
                "plate_number": vehicle["plate_number"] in body,
                "price_text": vehicle["price_text"] in body,
                "mileage_text": vehicle["mileage_text"] in body,
                "first_registered_month": vehicle["first_registered_month"] in body,
                "trim_group_or_raw": vehicle["trim_group"] in body or vehicle["trim_raw"] in body,
                "fuel_label": vehicle["fuel_label"] in body or vehicle["engine"] in body,
                "selectable_option_names": all(name in body for name in vehicle["selectable_option_names"]),
            }
            checked.append(
                {
                    "id": vehicle["id"],
                    "detail_url": vehicle["detail_url"],
                    "checks": checks,
                    "ok": all(checks.values()),
                }
            )
        browser.close()

    report = {
        "enabled": True,
        "sample_size": sample_size,
        "checked_count": len(checked),
        "ok": all(item["ok"] for item in checked),
        "checked": checked,
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Kia CPO Carnival listings and selected options.")
    parser.add_argument("--target-url", default=DEFAULT_TARGET_URL, help="Kia CPO products URL with filter=...")
    parser.add_argument("--output-dir", default="data", type=Path)
    parser.add_argument("--prefix", default="kia_cpo_carnival_7seat_sunroof")
    parser.add_argument("--limit", type=positive_int, default=None, help="Limit listing count for test runs.")
    parser.add_argument("--verify-dom", type=positive_int, default=0, help="Verify N scraped rows against rendered pages.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--headed", action="store_true", help="Show browser during DOM verification.")
    parser.add_argument("--retries", type=positive_int, default=3)
    parser.add_argument(
        "--update-db",
        type=Path,
        default=None,
        help="Update a CSV inventory DB keyed by plate_number, marking missing rows sold_out.",
    )
    parser.add_argument(
        "--db-report",
        type=Path,
        default=None,
        help="Write the DB update report JSON. Defaults to <db filename>.update.json when --update-db is used.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    client = KiaApiClient(retries=args.retries)
    paths = output_paths(args.output_dir, args.prefix)

    scraped_at = datetime.now(timezone.utc).isoformat()
    filter_params = parse_filter_params(args.target_url)
    listings = fetch_listing_rows(client, filter_params, args.limit)
    vehicles: list[dict[str, Any]] = []
    for index, listing in enumerate(listings, start=1):
        vehicle = fetch_vehicle(client, listing)
        vehicles.append(vehicle)
        print(
            f"[{index:02d}/{len(listings):02d}] {vehicle['id']} "
            f"{vehicle['plate_number']} {vehicle['price_text']} "
            f"{vehicle['first_registered_month']} {vehicle['mileage_text']} "
            f"{vehicle['fuel_label']} {vehicle['trim_group']} "
            f"선택옵션={', '.join(vehicle['selectable_option_names'])}"
        )

    write_outputs(vehicles, paths, args.target_url)
    db_report = None
    if args.update_db:
        db_report = update_csv_db(vehicles, args.update_db, scraped_at)
        db_report_path = args.db_report or args.update_db.with_suffix(".update.json")
        db_report_path.parent.mkdir(parents=True, exist_ok=True)
        db_report_path.write_text(json.dumps(db_report, ensure_ascii=False, indent=2), encoding="utf-8")
        db_report["report_path"] = str(db_report_path)
    verify_report = verify_with_playwright(
        vehicles,
        args.verify_dom,
        args.env_file,
        paths.verify_path,
        headless=not args.headed,
    )

    summary = summarize(vehicles)
    print("\nDone")
    print(f"count={summary['count']} by_trim={summary['by_trim']} by_fuel={summary['by_fuel']}")
    print(f"json={paths.json_path}")
    print(f"csv={paths.csv_path}")
    print(f"selectable_packages_csv={paths.selectable_packages_csv_path}")
    print(f"selectable_details_csv={paths.selectable_details_csv_path}")
    print(f"summary={paths.summary_path}")
    if db_report:
        print(
            f"db={db_report['path']} total_rows={db_report['total_rows']} "
            f"available={db_report['available_count']} sold_out={db_report['sold_out_count']} "
            f"added={len(db_report['added'])} newly_sold_out={len(db_report['sold_out'])} "
            f"reappeared={len(db_report['reappeared'])} report={db_report['report_path']}"
        )
    if verify_report.get("enabled"):
        print(f"verify_ok={verify_report['ok']} verify={paths.verify_path}")
        if not verify_report["ok"]:
            return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ScrapeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
