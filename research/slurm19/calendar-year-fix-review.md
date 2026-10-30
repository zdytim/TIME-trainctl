# Month conversion defect found in isolation

The isolated controller built from `2f57b32` reported month `202710` for timestamp `1793342846` (2026-10-30 UTC), while Beijing time was still October 2026. This was a real controller socket response, not a passing quota test. The civil-date inverse increments the year for March through December instead of January and February: `y += (mp < 10)`. This can assign accounting and grants to the wrong year and compute incorrect month boundaries. The staged correction uses `mp >= 10`, matching the transformed March-based calendar representation. The existing production controller was not changed.

The change must be compiled and checked at year and month boundaries, including Shanghai midnight, leap years and an actual isolated controller snapshot before any deployment. A matching source fix alone does not prove accounting, scheduling or recovery. No quota gate has been activated.
