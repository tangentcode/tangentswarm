"""Read any project board's open PR cards (tangentswarm #45: Project 6 runs the same flow).

The Project 4 readers (boardcache, promoted.read_board) keep their own snapshot; this light
reader serves the other boards: one paginated GraphQL read of the board's items, keeping
the OPEN pull requests of the board's repos.
"""
from . import board

ITEMS_Q = """query($org:String!,$n:Int!,$c:String){organization(login:$org){projectV2(number:$n){
 items(first:100, after:$c){pageInfo{hasNextPage endCursor} nodes{id
  fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name optionId}}
  content{__typename ...on PullRequest{number state isDraft headRefOid mergeable
   repository{nameWithOwner} milestone{title dueOn}
   closingIssuesReferences(first:5){nodes{number repository{nameWithOwner} milestone{title dueOn}}}}}}}}}}"""


def open_prs(gql, b, org='tangentcode'):
    """{column key: [{number, repo, head, item, draft, mergeable, closes, due, milestone}]} of
    the OPEN pull requests of board b's repos."""
    cols, cursor = {}, None
    while True:
        d = gql(ITEMS_Q, org=org, n=b.number, c=cursor)
        items = d['organization']['projectV2']['items']
        for it in items['nodes']:
            c = it.get('content') or {}
            repo = (c.get('repository') or {}).get('nameWithOwner')
            if c.get('__typename') != 'PullRequest' or c.get('state') != 'OPEN' or repo not in b.repos:
                continue
            cl = [i for i in (c.get('closingIssuesReferences') or {}).get('nodes') or []
                  if (i.get('repository') or {}).get('nameWithOwner') == repo]
            due, ms = board.soonest_due([c.get('milestone') or {}] + [i.get('milestone') or {} for i in cl])
            cols.setdefault(board.column(it.get('fieldValueByName')), []).append(dict(
                number=c['number'], repo=repo, head=c['headRefOid'], item=it['id'], draft=c['isDraft'],
                mergeable=c.get('mergeable'), closes=[i['number'] for i in cl], due=due, milestone=ms))
        if not items['pageInfo']['hasNextPage']:
            return cols
        cursor = items['pageInfo']['endCursor']
