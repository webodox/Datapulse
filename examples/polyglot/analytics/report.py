import pandas as pd
df = pd.read_csv("data/orders.csv")
total = df["amount"].sum()
