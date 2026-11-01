"""Cohort correlations from explicit run metadata; never infer causal attribution."""
import math

FIELDS = ('node_id', 'binary_version', 'bsp_version', 'scenario_id', 'model_version')


def correlate(sessions):
    groups = []
    completed = [s for s in sessions if s.status in ('completed', 'failed')]
    for field in FIELDS:
        values = sorted({str(s.metadata[field]) for s in completed if s.metadata.get(field) is not None})
        for value in values:
            cohort = [s for s in completed if str(s.metadata.get(field)) == value]
            failures = [s for s in cohort if s.status == 'failed']
            groups.append(dict(field=field, value=value, runs=len(cohort), failed=len(failures),
                               failure_rate=len(failures)/len(cohort), session_ids=[s.id for s in cohort], timing=timings(cohort),
                               matched_dimensions={key: sorted({str(s.metadata.get(key, 'unknown')) for s in cohort}) for key in FIELDS if key != field}))
    return dict(groups=groups, included_runs=len(completed), excluded_runs=len(sessions)-len(completed),
                missing_metadata_runs=sum(not any(k in s.metadata for k in FIELDS) for s in completed),
                warning='Observational association only. Scenario, binary, and worker assignment can confound rates; compare matched cohorts.')


def experiment_plan(baseline, candidate):
    differences = {k: [baseline.metadata.get(k), candidate.metadata.get(k)] for k in FIELDS if baseline.metadata.get(k) != candidate.metadata.get(k)}
    return dict(changed_dimensions=differences,
                controlled=len(differences)==1 and all(k in baseline.metadata and k in candidate.metadata for k in FIELDS),
                next_checks=['Match scenario, model, and binary before changing node/BSP.',
                             'Repeat independent trials and record success/failure; two observed runs do not establish a causal effect.'],
                warning='A matching plan, not an executed infrastructure experiment.')


def timings(sessions):
    result = {}
    for key in ('queue_wait_ms', 'download_ms', 'execution_ms'):
        values = sorted(e['payload'][key] for s in sessions for e in s.events
                        if e['kind'] == 'infra.metrics' and isinstance(e['payload'].get(key), (float, int))
                        and not isinstance(e['payload'][key], bool) and math.isfinite(e['payload'][key]) and e['payload'][key] >= 0)
        if values:
            result[key] = {'samples': len(values), 'mean': sum(values)/len(values),
                           'p95_nearest_rank': values[max(0, math.ceil(.95*len(values))-1)]}
    return result
