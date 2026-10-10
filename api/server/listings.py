"""Read-only approved token identities; listing administration is private."""
import json
from .store import db
def approved():
    with db() as c: rows=c.execute("SELECT * FROM listing_requests WHERE status='listed' ORDER BY created").fetchall()
    result=[]
    for row in rows:
        p=json.loads(row['payload'])
        result.append({'id':row['mint'],'mint':row['mint'],'symbol':row['symbol'],'name':row['name'],
                       'verification':{'status':'reviewed','id':row['mint'],'mint':row['mint'],
                                       'label':'Reviewed by ticking','scope':'Token identity and listing-source review.',
                                       'reviewed_at':row['updated'],'basis':'operator_listing_review'},
                       'website':p['website'],'x':p['x'],'category':p['category'],'coingecko':None,'revenue':None,'reviewed_at':row['updated']})
    return result
