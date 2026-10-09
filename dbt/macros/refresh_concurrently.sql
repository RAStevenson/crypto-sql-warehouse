{#
  dbt-postgres refreshes a materialized view with a plain REFRESH, which
  takes a lock that blocks every reader until the refresh commits. A
  connected Tableau session would stall for the length of the refresh. This
  override adds CONCURRENTLY, so readers keep seeing the old contents until
  the new ones commit. CONCURRENTLY needs a unique index on the view, which
  each of the three materialized views under models/marts/ declares in its
  config.

  A project macro with the adapter's own name replaces the adapter's.
#}
{% macro postgres__refresh_materialized_view(relation) %}
    refresh materialized view concurrently {{ relation }}
{% endmacro %}
