# Data

## Local files

- Source archive: `data/raw/take-home-project.zip`
- Extracted Parquet: `data/extracted/take-home-project/data/`
- Source README: `data/extracted/take-home-project/README.md`

The notebook finds this directory when launched from either the project root or `notebooks/`.

## Archive verification

- Size: 152,007,673 bytes
- SHA-256: `965f70c1be3ff204f0d7a9033b147f873f66192f0adcf203f6f762a7e1e7c80b`
- `unzip -t`: passed
- Extracted size: approximately 172 MiB

## Contents

| Type | Rows | Description |
|---|---:|---|
| Order book | 3,581,577 | 20 bid/ask levels; nanosecond timestamps |
| Trades | 70,556 | Price, size, and aggressor direction |
| Funding | 12,902 | Funding estimate/rate, updated roughly every 20 seconds |

## Memory behavior

The simulator reads every order-book row through `pyarrow.parquet.ParquetFile.iter_batches()` in batches of 100,000. It retains only current market state, fills, active orders, and approximately one monitoring row per second. The EDA notebook separately resamples the book for aggregate analysis.
