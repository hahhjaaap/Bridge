"""Project-independent names for data/backup containers in scoped source APIs."""

DATA_CONTAINER_SUFFIXES = (
    "_datasets",
    "-datasets",
    "_backups",
    "-backups",
    "_quarantine",
    "-quarantine",
)


def is_data_container(name: str) -> bool:
    return name.casefold().endswith(DATA_CONTAINER_SUFFIXES)
