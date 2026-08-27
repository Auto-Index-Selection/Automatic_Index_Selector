from pathlib import Path
from pyprojroot import here

job_schema = {
    "aka_name": {
        "id": "INT",
        "person_id": "INT",
        "name": "VARCHAR",
        "imdb_index": "VARCHAR",
        "name_pcode_cf": "VARCHAR",
        "name_pcode_nf": "VARCHAR",
        "surname_pcode": "VARCHAR",
        "md5sum": "VARCHAR"
    },
    "aka_title": {
        "id": "INT",
        "movie_id": "INT",
        "title": "VARCHAR",
        "imdb_index": "VARCHAR",
        "kind_id": "INT",
        "production_year": "INT",
        "phonetic_code": "VARCHAR",
        "episode_of_id": "INT",
        "season_nr": "INT",
        "episode_nr": "INT",
        "note": "VARCHAR",
        "md5sum": "VARCHAR"
    },
    "cast_info": {
        "id": "INT",
        "person_id": "INT",
        "movie_id": "INT",
        "person_role_id": "INT",
        "note": "VARCHAR",
        "nr_order": "INT",
        "role_id": "INT"
    },
    "char_name": {
        "id": "INT",
        "name": "VARCHAR",
        "imdb_index": "VARCHAR",
        "imdb_id": "INT",
        "name_pcode_nf": "VARCHAR",
        "surname_pcode": "VARCHAR",
        "md5sum": "VARCHAR"
    },
    "comp_cast_type": {
        "id": "INT",
        "kind": "VARCHAR"
    },
    "company_name": {
        "id": "INT",
        "name": "VARCHAR",
        "country_code": "VARCHAR",
        "imdb_id": "INT",
        "name_pcode_nf": "VARCHAR",
        "name_pcode_sf": "VARCHAR",
        "md5sum": "VARCHAR"
    },
    "company_type": {
        "id": "INT",
        "kind": "VARCHAR"
    },
    "complete_cast": {
        "id": "INT",
        "movie_id": "INT",
        "subject_id": "INT",
        "status_id": "INT"
    },
    "info_type": {
        "id": "INT",
        "info": "VARCHAR"
    },
    "keyword": {
        "id": "INT",
        "keyword": "VARCHAR",
        "phonetic_code": "VARCHAR"
    },
    "kind_type": {
        "id": "INT",
        "kind": "VARCHAR"
    },
    "link_type": {
        "id": "INT",
        "link": "VARCHAR"
    },
    "movie_companies": {
        "id": "INT",
        "movie_id": "INT",
        "company_id": "INT",
        "company_type_id": "INT",
        "note": "VARCHAR"
    },
    "movie_info": {
        "id": "INT",
        "movie_id": "INT",
        "info_type_id": "INT",
        "info": "VARCHAR",
        "note": "VARCHAR"
    },
    "movie_info_idx": {
        "id": "INT",
        "movie_id": "INT",
        "info_type_id": "INT",
        "info": "VARCHAR",
        "note": "VARCHAR"
    },
    "movie_keyword": {
        "id": "INT",
        "movie_id": "INT",
        "keyword_id": "INT"
    },
    "movie_link": {
        "id": "INT",
        "movie_id": "INT",
        "linked_movie_id": "INT",
        "link_type_id": "INT"
    },
    "name": {
        "id": "INT",
        "name": "VARCHAR",
        "imdb_index": "VARCHAR",
        "imdb_id": "INT",
        "gender": "VARCHAR",
        "name_pcode_cf": "VARCHAR",
        "name_pcode_nf": "VARCHAR",
        "surname_pcode": "VARCHAR",
        "md5sum": "VARCHAR"
    },
    "person_info": {
        "id": "INT",
        "person_id": "INT",
        "info_type_id": "INT",
        "info": "VARCHAR",
        "note": "VARCHAR"
    },
    "role_type": {
        "id": "INT",
        "role": "VARCHAR"
    },
    "title": {
        "id": "INT",
        "title": "VARCHAR",
        "imdb_index": "VARCHAR",
        "kind_id": "INT",
        "production_year": "INT",
        "imdb_id": "INT",
        "phonetic_code": "VARCHAR",
        "episode_of_id": "INT",
        "season_nr": "INT",
        "episode_nr": "INT",
        "series_years": "VARCHAR",
        "md5sum": "VARCHAR"
    }
}


def getWorkload():
    workloadPath = str(here() / "workload" / "queries_job")
    queries = []
    workloadPath = Path(workloadPath)
    for sql_file in sorted(workloadPath.glob("*.sql")):

        with open(sql_file, "r") as f:
            query = f.read().strip()

        if query:
            queries.append(query)

    return queries, 'job', job_schema