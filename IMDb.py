#!/usr/bin/env python3
"""
IMDb Top Movies/TV Shows Data Generator

Original Post: https://medium.com/@nishantsahoo/which-movie-should-i-watch-5c83a3c0f5b1
Author: Jugal Kishore
Version: 4.0
"""

import csv
import gzip
import html
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import requests

# Original post link
ORIGINAL_POST_URL = (
    "https://medium.com/@nishantsahoo/which-movie-should-i-watch-5c83a3c0f5b1"
)

# Get current year
CURRENT_YEAR = datetime.now().year

CACHE_TTL_DAYS = 7

# IMDb's own chart/search pages are protected by AWS WAF Bot Control and
# can't be scraped reliably (see project history). Rankings below are
# recreated from IMDb's public datasets instead; per-title detail (plot,
# cast, poster, box office, etc.) comes from the OMDb API.
IMDB_DATASETS_BASE = "https://datasets.imdbws.com"
TITLE_BASICS_URL = f"{IMDB_DATASETS_BASE}/title.basics.tsv.gz"
TITLE_RATINGS_URL = f"{IMDB_DATASETS_BASE}/title.ratings.tsv.gz"

IMDB_BASE_URL = "https://www.imdb.com"
IMDB_TOP_250_MOVIES_URL = f"{IMDB_BASE_URL}/chart/top/"
IMDB_TOP_250_TV_URL = f"{IMDB_BASE_URL}/chart/toptv/"
IMDB_MOVIES_SEARCH_URL = f"{IMDB_BASE_URL}/search/title/?title_type=feature&release_date={CURRENT_YEAR}-01-01,{CURRENT_YEAR}-12-31"
IMDB_TV_SEARCH_URL = f"{IMDB_BASE_URL}/search/title/?title_type=tv_series&release_date={CURRENT_YEAR}-01-01,{CURRENT_YEAR}-12-31"

# Minimum vote counts before a title is eligible for the recreated Top 250
# lists (keeps low-sample outlier ratings out of the results).
MOVIE_VOTE_THRESHOLD = 25000
TV_VOTE_THRESHOLD = 5000
TOP_N = 250
YEAR_TOP_N = 50

OMDB_API_URL = "https://www.omdbapi.com/"
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "")

_ENRICH_FIELDS = {
    "certificate", "plot", "image", "director", "writer", "actors", "awards",
    "boxOffice", "country", "language", "rottenTomatoes", "metacritic",
    "last_updated",
}


def download_dataset(url: str, dest_path: str) -> None:
    print(f"  Downloading {url} ...")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)


def load_ratings(path: str) -> dict:
    ratings = {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            try:
                ratings[row["tconst"]] = (float(row["averageRating"]), int(row["numVotes"]))
            except ValueError:
                continue
    return ratings


def build_rankings(basics_path: str, ratings: dict) -> dict:
    """
    Stream title.basics.tsv.gz once, bucketing titles into the four ranking
    pools we publish. IMDb's real Top 250 / popularity-meter formulas aren't
    public, so these are a rating+votes recreation, not an exact mirror.
    """
    top250_movies, top250_tv = [], []
    top50_movies_year, top50_tv_year = [], []

    with gzip.open(basics_path, "rt", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            rating = ratings.get(row["tconst"])
            if not rating or row["isAdult"] == "1":
                continue

            title_type = row["titleType"]
            start_year = row["startYear"]
            avg, votes = rating

            is_movie = title_type == "movie"
            is_tv = title_type in ("tvSeries", "tvMiniSeries")
            if not is_movie and not is_tv:
                continue

            rec = {
                "tconst": row["tconst"],
                "name": row["primaryTitle"],
                "year": start_year if start_year != "\\N" else "",
                "rating": avg,
                "votes": votes,
                "genres": row["genres"] if row["genres"] != "\\N" else "",
                "runtime": int(row["runtimeMinutes"]) if row["runtimeMinutes"].isdigit() else 0,
                "titleType": title_type,
            }

            if is_movie and votes >= MOVIE_VOTE_THRESHOLD:
                top250_movies.append(rec)
            if is_tv and votes >= TV_VOTE_THRESHOLD:
                top250_tv.append(rec)
            if start_year == str(CURRENT_YEAR):
                (top50_movies_year if is_movie else top50_tv_year).append(dict(rec))

    top250_movies.sort(key=lambda r: (-r["rating"], -r["votes"]))
    top250_tv.sort(key=lambda r: (-r["rating"], -r["votes"]))
    top50_movies_year.sort(key=lambda r: -r["votes"])
    top50_tv_year.sort(key=lambda r: -r["votes"])

    return {
        "top250_movies": top250_movies[:TOP_N],
        "top250_tv": top250_tv[:TOP_N],
        "top50_movies_year": top50_movies_year[:YEAR_TOP_N],
        "top50_tv_year": top50_tv_year[:YEAR_TOP_N],
    }


def load_rankings() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        ratings_path = os.path.join(tmp, "title.ratings.tsv.gz")
        basics_path = os.path.join(tmp, "title.basics.tsv.gz")
        download_dataset(TITLE_RATINGS_URL, ratings_path)
        download_dataset(TITLE_BASICS_URL, basics_path)

        print("  Loading ratings...")
        ratings = load_ratings(ratings_path)
        print(f"  {len(ratings):,} rated titles loaded.")

        print("  Scanning title basics and building rankings...")
        return build_rankings(basics_path, ratings)


def _unescape(value):
    return html.unescape(value) if isinstance(value, str) else value


def _na(value):
    return "" if value in (None, "N/A") else value


def fetch_omdb(tconst: str) -> dict:
    try:
        resp = requests.get(
            OMDB_API_URL, params={"i": tconst, "apikey": OMDB_API_KEY}, timeout=15
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"    Warning: OMDb lookup failed for {tconst}: {e}")
        return {}

    if data.get("Response") != "True":
        print(f"    Warning: OMDb has no data for {tconst}: {data.get('Error', '')}")
        return {}

    rotten_tomatoes = metacritic = ""
    for r in data.get("Ratings", []):
        if r.get("Source") == "Rotten Tomatoes":
            rotten_tomatoes = r.get("Value", "")
        elif r.get("Source") == "Metacritic":
            metacritic = r.get("Value", "")

    return {
        "certificate": _na(data.get("Rated")),
        "plot": _unescape(_na(data.get("Plot"))),
        "image": _na(data.get("Poster")),
        "director": _unescape(_na(data.get("Director"))),
        "writer": _unescape(_na(data.get("Writer"))),
        "actors": _unescape(_na(data.get("Actors"))),
        "awards": _unescape(_na(data.get("Awards"))),
        "boxOffice": _na(data.get("BoxOffice")),
        "country": _na(data.get("Country")),
        "language": _na(data.get("Language")),
        "rottenTomatoes": rotten_tomatoes,
        "metacritic": metacritic,
    }


def _title_id_from_link(link: str) -> str:
    parts = [p for p in link.rstrip("/").split("/") if p.startswith("tt")]
    return parts[0] if parts else link


def enrich_items(items: list[dict], existing: list[dict] = None) -> list[dict]:
    """
    Enrich a list of title dicts via the OMDb API. Reuses enrichment fields
    from `existing` if last_updated is within CACHE_TTL_DAYS, since OMDb's
    free tier is rate-limited to 1000 requests/day.
    """
    if not items:
        return items

    cache = {}
    if existing:
        for ex in existing:
            link = ex.get("link", "")
            if link:
                cache[_title_id_from_link(link)] = {
                    k: v for k, v in ex.items() if k in _ENRICH_FIELDS
                }

    now = datetime.now()
    ttl = timedelta(days=CACHE_TTL_DAYS)

    to_fetch = []
    for item in items:
        cached = cache.get(item["tconst"])
        if cached:
            last_updated = cached.get("last_updated", "")
            if last_updated:
                try:
                    if now - datetime.fromisoformat(last_updated) < ttl:
                        item.update(cached)
                        continue
                except ValueError:
                    pass
        to_fetch.append(item)

    cached_count = len(items) - len(to_fetch)
    if cached_count:
        print(f"  {cached_count} items served from cache.")
    if to_fetch:
        print(f"  Fetching {len(to_fetch)} items from OMDb...")
        for i, item in enumerate(to_fetch):
            print(f"  Enriching ({i + 1}/{len(to_fetch)}): {item['name']}")
            enrichment = fetch_omdb(item["tconst"])
            enrichment["last_updated"] = now.isoformat()
            item.update(enrichment)

    return items


def finalize(pool: list[dict], existing: list[dict] = None) -> list[dict]:
    for rec in pool:
        rec["link"] = f"{IMDB_BASE_URL}/title/{rec['tconst']}/"

    enrich_items(pool, existing=existing)

    result = []
    for rank, rec in enumerate(pool, 1):
        del rec["tconst"]
        result.append({"Rank": rank, **rec})
    return result


def fetch_top_50_movies(rankings: dict, existing: list[dict] = None) -> list[dict]:
    print(f"Building Top 50 Movies {CURRENT_YEAR} (recreated from IMDb datasets)")
    return finalize(rankings["top50_movies_year"], existing=existing)


def fetch_top_250_movies(rankings: dict, existing: list[dict] = None) -> list[dict]:
    print("Building Top 250 Movies (recreated from IMDb datasets)")
    return finalize(rankings["top250_movies"], existing=existing)


def fetch_top_50_shows(rankings: dict, existing: list[dict] = None) -> list[dict]:
    print(f"Building Top 50 TV Shows {CURRENT_YEAR} (recreated from IMDb datasets)")
    return finalize(rankings["top50_tv_year"], existing=existing)


def fetch_top_250_tv(rankings: dict, existing: list[dict] = None) -> list[dict]:
    print("Building Top 250 TV Shows (recreated from IMDb datasets)")
    return finalize(rankings["top250_tv"], existing=existing)


def print_top_50_movies(movies_data):
    """
    Print the Top 50 Movie names.

    Args:
        movies_data (list of dict): A list where each dictionary contains movie information,
        such as the Movie's name and link.
    """
    import subprocess

    file = open("temp.csv", "w")
    file.write("Rank; Movie Name; Movie Link\n\n")
    for i, movie in enumerate(movies_data[:50], 1):
        file.write(f'"{i}"; "{movie["name"]}"; "{movie["link"]}"\n')
    file.close()

    subprocess.call(["csvtomd", "-d", ";", "temp.csv"])
    os.remove("temp.csv")


def ensure_path_directory(full_path):
    """
    Function to ensure that the directory exists for any given path.

    Args:
        full_path (str): The full path of the directory.
    """
    directory = os.path.dirname(full_path)
    if not os.path.exists(directory):
        os.makedirs(directory)


def save_to_json(fetched_data, file_path):
    ensure_path_directory(file_path)
    if not fetched_data:
        return
    with open(file_path, "w") as f:
        json.dump(fetched_data, f, indent=2)


def save_to_csv(fetched_data, file_path, content_type):
    """
    Save fetched data to a CSV file.

    Args:
        fetched_data (list of dict): A list where each dictionary contains Movie/Show information,
        such as the Movie/Show's name and link.
        file_path (str): The file path where the data should be saved.
    """
    ensure_path_directory(file_path)

    if not fetched_data:
        return

    keys = [k for k in dict.fromkeys(k for d in fetched_data for k in d.keys()) if k != "last_updated"]
    header = ", ".join(keys)

    file = open(file_path, "w")
    file.write(header + "\n\n")
    file.close()

    file = open(file_path, "a")
    for item in fetched_data:
        values = [str(item.get(k, "")) for k in keys]
        file.write(", ".join(f'"{v}"' for v in values) + "\n")
    file.close()


def save_to_md(fetched_data):
    """
    Save Movie data to a Markdown file.

    Args:
        fetched_data (list of dict): A list where each dictionary contains Movie/Show information,
        such as the Movie/Show's name and link.
    """
    file = open("README.md", "w")
    file.write("# IMDb Top 50 & 250 Movie/TV Show Data Scraper\n\n")
    file.close()

    file = open("README.md", "a")
    file.write(f"## Original Medium Post: [Link]({ORIGINAL_POST_URL})\n")
    file.write(f"\n**Top IMDb Movies as of:** {datetime.now().date()}\n\n")
    file.write(
        "> Rankings are recreated from IMDb's public datasets "
        "([datasets.imdbws.com](https://datasets.imdbws.com/)) - IMDb's live "
        "chart/search pages are protected against automated access. Per-title "
        "details (plot, cast, poster, box office, etc.) come from the "
        "[OMDb API](https://www.omdbapi.com/).\n\n"
    )
    file.write(
        "**Top 50 Movies:** [CSV File](/data/top50/movies.csv), [JSON File](/data/top50/movies.json)\n\n"
    )
    file.write(
        "**Top 250 Movies:** [CSV File](/data/top250/movies.csv), [JSON File](/data/top250/movies.json)\n\n"
    )
    file.write(
        "**Top 50 TV Shows:** [CSV File](/data/top50/shows.csv), [JSON File](/data/top50/shows.json)\n\n"
    )
    file.write(
        "**Top 250 TV Shows:** [CSV File](/data/top250/shows.csv), [JSON File](/data/top250/shows.json)\n\n"
    )
    file.write(
        "**Popular Movies / Popular TV Shows:** [data/popular/](/data/popular/) - "
        "stale, no longer updated (IMDb's popularity-meter ranking isn't public data).\n\n"
    )
    file.write("---\n\n")
    file.write("## IMDb Top 50 Movies List\n\n")

    for i, item in enumerate(fetched_data, 1):
        file.write(f"{i}. [{item['name']}]({item['link']})\n\n")

    file.close()


def _load_json(path: str) -> list[dict]:
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return []


if __name__ == "__main__":
    print("/// IMDb Top 50 & 250 Movie/TV Show Data Generator ///\n")
    print(f"Original Medium Post: {ORIGINAL_POST_URL}\n")

    if not OMDB_API_KEY:
        print("ERROR: OMDB_API_KEY environment variable is not set.")
        sys.exit(1)

    print("--- Downloading & ranking IMDb datasets ---")
    rankings = load_rankings()
    print("  Done.")

    print("\n--- 1. Top 50 Movies ---")
    fetched_movies = fetch_top_50_movies(rankings, existing=_load_json("data/top50/movies.json"))
    save_to_json(fetched_movies, "data/top50/movies.json")
    save_to_csv(fetched_movies, "data/top50/movies.csv", "movies")
    save_to_md(fetched_movies)
    print("  Done.")

    print("\n--- 2. Top 250 Movies ---")
    fetched_top250_movies = fetch_top_250_movies(rankings, existing=_load_json("data/top250/movies.json"))
    save_to_json(fetched_top250_movies, "data/top250/movies.json")
    save_to_csv(fetched_top250_movies, "data/top250/movies.csv", "movies")
    print("  Done.")

    print("\n--- 3. Top 50 TV Shows ---")
    fetched_shows = fetch_top_50_shows(rankings, existing=_load_json("data/top50/shows.json"))
    save_to_json(fetched_shows, "data/top50/shows.json")
    save_to_csv(fetched_shows, "data/top50/shows.csv", "shows")
    print("  Done.")

    print("\n--- 4. Top 250 TV Shows ---")
    fetched_top250_shows = fetch_top_250_tv(rankings, existing=_load_json("data/top250/shows.json"))
    save_to_json(fetched_top250_shows, "data/top250/shows.json")
    save_to_csv(fetched_top250_shows, "data/top250/shows.csv", "shows")
    print("  Done.")

    if sys.version_info < (3, 10):
        print("\nPrinting Top 50 Movies (Python < 3.10 format):")
        print_top_50_movies(fetched_movies)
