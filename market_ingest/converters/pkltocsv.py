import pickle
import pandas as pd

with open(r"C:\Users\Vinay\Desktop\derivix\databuildup\study\market_ingest\converters\NIFTY NE 2024-01-01.pkl", "rb") as f:
    obj = pickle.load(f)

print(type(obj))

# If it's a DataFrame
if isinstance(obj, pd.DataFrame):
    obj.to_csv("output.csv", index=False)
    print("DataFrame saved to output.csv")

# If it's a Series
elif isinstance(obj, pd.Series):
    obj.to_frame().to_csv("output.csv", index=False)
    print("Series saved to output.csv")

# If it's a list or dictionary
else:
    pd.DataFrame(obj).to_csv("output.csv", index=False)
    print("Converted object to DataFrame and saved to output.csv")