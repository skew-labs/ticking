"""Shared UTC candle alignment. Solana trading weeks open Monday 00:00 UTC."""
WEEK=604800
WEEK_ORIGIN=-259200  # Monday 1969-12-29, before all supported chain timestamps.

def origin(period):return WEEK_ORIGIN if period==WEEK else 0
def bucket_start(stamp,period):return (stamp-origin(period))//period*period+origin(period)
def bucket_sql(period):
    if type(period)!=int or not 1<=period<=WEEK:raise ValueError('Unsupported candle period')
    anchor=origin(period)
    return f'((time-({anchor}))/{period}*{period}+({anchor}))'
