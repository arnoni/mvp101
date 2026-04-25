# Input Format Stats Daily - Dev Notes

## Distribution by input format

```sql
SELECT
    input_format,
    SUM(count) AS total_count
FROM input_format_stats_daily
GROUP BY input_format
ORDER BY total_count DESC;
```

## Distribution by target mode

```sql
SELECT
    target_mode,
    input_format,
    SUM(count) AS total_count
FROM input_format_stats_daily
GROUP BY target_mode, input_format
ORDER BY target_mode, total_count DESC;
```

## Unsupported or failed input styles

```sql
SELECT
    input_format,
    input_parse_status,
    input_host,
    SUM(count) AS total_count
FROM input_format_stats_daily
WHERE input_parse_status <> 'parsed'
GROUP BY input_format, input_parse_status, input_host
ORDER BY total_count DESC;
```

## Google short-link demand

```sql
SELECT
    stat_date,
    target_mode,
    user_state,
    SUM(count) AS total_count
FROM input_format_stats_daily
WHERE input_format = 'google_maps_short_url'
GROUP BY stat_date, target_mode, user_state
ORDER BY stat_date DESC, total_count DESC;
```

## Daily trend

```sql
SELECT
    stat_date,
    input_format,
    SUM(count) AS total_count
FROM input_format_stats_daily
GROUP BY stat_date, input_format
ORDER BY stat_date DESC, total_count DESC;
```
