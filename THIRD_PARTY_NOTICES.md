# Third-party services and notices

This project is MIT-licensed (see `LICENSE`) and uses only the Python standard library. It bundles
no third-party code. It does call third-party services and tools at run time, which you must
provide and which have their own terms.

## TypeSafe (commercial API)

`ticket_triage/jev.py` calls TypeSafe's System One API (`https://api.typesafe.ai/v1/systemone`,
model `jev-latest` unless you pass `--model`). TypeSafe is a commercial, paid, third-party
service. No TypeSafe code, SDK or model is included in this repository; the client is a plain HTTPS
request written against the public API documentation. You need your own account and your own
`TYPESAFE_API_KEY`, and you are responsible for any charges. This project is not affiliated with or
endorsed by TypeSafe, and "TypeSafe" and "Jev" are used here only to say which service the code calls.

### What is sent to TypeSafe

`python -m ticket_triage.triage`, for every issue it classifies:

- the issue's title, description (cut at any `--cut-description` match, then at 3,500 characters),
  workflow state and project name
- up to the last 8 comments, 600 characters each, minus those matching `--ignore-comment`.
  Comment authors are replaced with pseudonyms ("person 1", "person 2", ... per issue) unless you
  pass `--include-comment-authors`, in which case their display names are sent
- the issue's label names, only if you pass `--include-labels`
- the repository description from `--repository` (`name`, `contains`, `does_not_contain`, and the
  `deploy_includes` text inside the question wording)
- code evidence computed locally, unless `--no-evidence`: file paths and symbol names the ticket
  mentions with whether and where they exist on the base branch, and the titles and states of pull
  requests and the subjects of commits that name the issue id

`python -m ticket_triage.label`:

- `--ids` and `--backtest`: the same issue fields as above, never the issue's labels
- `--relabel`: the description of each merged pull request (`pr-body.md`, up to 6,000 characters)
- the group questions, label descriptions and dimension wording from your taxonomy file

`--audit` and `--calibrate-effort` read local files only and send nothing.

Before you run either tool, check TypeSafe's current terms and data-handling documentation, and
your own organisation's policy on sending ticket text, colleagues' comments and code metadata to an
external processor. Do not run it over tickets that are not allowed to leave your systems.

Output files (`results.json`, `report.md`, `eval-*.json`, backtest files) contain ticket text and
raw model outputs. Keep them private; the shipped `.gitignore` excludes the default locations.

## Linear

`ticket_triage/linear.py` reads issues through Linear's GraphQL API
(`https://api.linear.app/graphql`) with your `LINEAR_API_KEY`. It never writes to Linear.

## git and the GitHub CLI

`ticket_triage/evidence.py` runs your local `git` and, for pull request evidence, an authenticated
`gh` (GitHub CLI). Both are used as installed programs; neither is bundled.
