import csv
from collections import Counter

def get_ids(filepath):
    ids = Counter()
    with open(filepath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            req = row['Requirements'].strip()
            if req and req != '-':
                for r in req.split(','):
                    ids[r.strip()] += 1
    return ids

# Get IDs from the full course catalog
all_ids = get_ids('database/courses-report.2026-06-09.csv')

# For each LE requirement, find IDs that appear proportionally MORE
# in the filtered set than in the full catalog
files = {
    'Arts/Humanities':      'database/courses-report.2026-06-10.Arts.csv',
    'Biological Sciences':  'database/courses-report.2026-06-10.Bio.csv',
    'Civic Life and Ethics':'database/courses-report.2026-06-09.Civic.csv',
    'Global Perspectives':  'database/courses-report.2026-06-10.Global.csv',
    'Historical Perspectives': 'database/courses-report.2026-06-10.Hist.csv',
    'Literature':           'database/courses-report.2026-06-10.Lit.csv',
    'Mathematical Thinking':'database/courses-report.2026-06-10.Math.csv',
    'Physical Sciences':    'database/courses-report.2026-06-10.Phys.csv',
    'Race Power Justice':   'database/courses-report.2026-06-10.Race.csv',
    'Social Sciences':      'database/courses-report.2026-06-10.Soc.csv',
    'The Environment':      'database/courses-report.2026-06-10.Env.csv',
    'Technology and Society': 'database/courses-report.2026-06-10.Tech.csv',
}

total_courses = sum(1 for _ in open('database/courses-report.2026-06-09.csv')) - 1

for name, path in files.items():
    le_ids = get_ids(path)
    le_total = sum(1 for _ in open(path)) - 1

    # Score each ID by how much more common it is in this LE set vs overall
    scores = {}
    for id_, le_count in le_ids.items():
        overall_count = all_ids.get(id_, 0)
        le_rate = le_count / le_total
        overall_rate = overall_count / total_courses
        if overall_rate > 0:
            scores[id_] = le_rate / overall_rate
        else:
            scores[id_] = float('inf')

    # Top scoring ID is most specific to this LE requirement
    top = sorted(scores.items(), key=lambda x: -x[1])[:3]
    print(f'\n{name}:')
    for id_, score in top:
        print(f'  {id_}: score={score:.1f}, in_le={le_ids[id_]}/{le_total}')