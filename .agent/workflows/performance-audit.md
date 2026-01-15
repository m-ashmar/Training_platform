---
description: Performance and scalability audit for the Training Platform
---

# Performance Audit Workflow

## 1. Database Layer Analysis
- Check `settings.py` for database configuration (engine, connection pooling, CONN_MAX_AGE)
- Review all models for missing indexes on frequently queried fields
- Search for N+1 query patterns using grep: `for .+\.all\(\)`
- Verify `select_related` and `prefetch_related` usage in ViewSets

## 2. Query Pattern Review
- Analyze all ViewSet `queryset` definitions for optimization
- Check serializer methods for database queries (SerializerMethodField)
- Look for loops that trigger individual database queries
- Verify `bulk_create`/`bulk_update` usage for batch operations

## 3. Caching Strategy
// turbo
- Review CACHES configuration in settings.py
- Check for LocMemCache (not production-ready) vs Redis
- Analyze CacheMiddleware cacheable paths
- Search for `cache.get`/`cache.set` usage patterns
- Identify data appropriate for caching (static, user-scoped)

## 4. Async/Background Processing
- Check Celery configuration and task definitions
- Identify blocking operations that should be async
- Review signal handlers for expensive operations
- Look for synchronous external API calls in request path

## 5. API Response Analysis
- Check pagination implementation
- Review large serializer response sizes
- Verify computed fields are optimal
- Look for redundant data in responses

## 6. Generate Report
- Create detailed audit report with findings
- Categorize by priority (Critical, High, Medium)
- Include code location references
- Provide concrete remediation recommendations
