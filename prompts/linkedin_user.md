Write one LinkedIn post from the activity below.

Author GitHub username: {username}

Overall score: {score}/100
High-confidence threshold: {high_confidence_threshold}
Draft threshold: {draft_threshold}
High confidence: {high_confidence}

Lead event
- Type: {lead_type}
- New repository: {lead_is_new_repo}
- Repo: {lead_repo}
- Title: {lead_title}
- URL: {lead_url}
- When: {lead_when}
- Lines changed: +{lead_additions} / -{lead_deletions} across {lead_files} files
- Stars / forks: {lead_stars} / {lead_forks}
- Conventional commit type: {lead_conventional}
- Breaking change: {lead_breaking}
- Positive keywords: {lead_positive_keywords}
- Negative keywords: {lead_negative_keywords}

Lead body / notes:
{lead_body}

Lead commits:
{lead_commits}

Score breakdown (points contributing to {score}):
{score_breakdown}

Supporting events in the same repo (context only — do not write a separate post for each):
{supporting}

Repo description: {repo_description}

Repo README (generation context — the thesis lives here when the event body is thin; not a changelog):
{repo_readme}

If New repository is yes: write the bet. Do not inventory files. Do not write a generic explainer for a protocol named in the brief. Lead with why the repo exists. Use the README as the spine if it has one.

Remember: facts only from this brief. First person. 150–350 words. JSON with post_text and reasoning.
