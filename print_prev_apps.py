import db
conn = db.get_connection()
cursor = conn.cursor()
cursor.execute('SELECT company, title FROM jobs WHERE status IN ("applied", "interviewing", "offer", "rejected")')
apps = cursor.fetchall()
print(f"Count: {len(apps)}")
for a in apps[:10]:
    print(a)
