import pandas as pd
import os
import csv

def investment_summary(dir):

    # Define functions: ---------------------------------------------------------------------------------------------

    def file_is_nonempty_csv(path):
        if not os.path.isfile(path):
            return False
        with open(path, newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            rows = list(reader)
            return len(rows) > 1  # i.e. at least one data row

    def import_and_keep_only_CH(filepath, index_col=0, usecols=None):
        if os.path.isfile(filepath):
            # an empty export (par_var writes a component without entries as "") has no rows
            if not file_is_nonempty_csv(filepath):
                return pd.DataFrame()
            # Import the data
            try:
                df = pd.read_csv(filepath, index_col=index_col, usecols=usecols)
            except ValueError as error:
                raise ValueError(f"{filepath}: {error}") from error
            # Keep only the rows of plants at a Swiss node, whatever their name (e.g. large_psp at CH00)
            names = df.index.astype(str)
            unmapped = sorted(set(names) - set(node_of))
            if unmapped:
                raise ValueError(f"{filepath}: no node in Map_node_plant.csv or Map_infeedplant_node.csv for "
                                 f"{len(unmapped)} entries, e.g. {unmapped[:5]}")
            return df.loc[names.isin(swiss)]
        else:
            return pd.DataFrame()
    
    def safe_read_csv(path, **kwargs):
        if not file_is_nonempty_csv(path):
            return pd.DataFrame()

        try:
            df = pd.read_csv(path, **kwargs)
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
        except (KeyError, IndexError):
            return pd.DataFrame()

        return df

    node_of, swiss = plants_at_swiss_nodes(dir)

    # Import the data: ----------------------------------------------------------------------------------------------

    # Existing power (MW):
    gen_max_infeedpower = import_and_keep_only_CH(dir + "gen_max_infeedp.csv", usecols=[0, 2])

    # Added Power (MW):
    gen_max = import_and_keep_only_CH(dir + "gen_max.csv", usecols=[0, 2])
    genTh_max = safe_read_csv(dir + "genTh_max.csv", index_col=0, usecols=["PDH", "value"])

    # New Power (MW):

    # Max New Power (MW):
    gen_max_limit = import_and_keep_only_CH(dir + "gen_max_limit.csv")
    genTh_max_limit = safe_read_csv(dir + "genTh_max_limit.csv", index_col=0)

    # Existing Storage (MWh):

    # Added Storage (MWh):

    # New Storage (MWh):
    fuel_storage_capacity_annual = safe_read_csv(dir + "fuel_storage_capacity_annual.csv", index_col=0, usecols=[0, 2])
    gen_energy_max = import_and_keep_only_CH(dir + "gen_energy_max.csv", usecols=[0, 2])
    gen_energyTh_max = safe_read_csv(dir + "gen_energyTh_max.csv", index_col=0, usecols=[0, 2])

    # Max New Storage (MWh):
    fuel_storage_investment_annual_limit = safe_read_csv(dir + "fuel_storage_investment_annual_limit.csv", index_col=0, usecols=[0, 2])
    energy_max_limit = import_and_keep_only_CH(dir + "energy_max_limit.csv")
    energyTh_max_limit = safe_read_csv(dir + "energyTh_max_limit.csv", index_col=0)

    # Investment Cost (CHF/MW):
    investment_genmax_slp = import_and_keep_only_CH(dir + "investment_genmax_slp.csv", usecols=[0, 2])
    investment_genmax_slpTh = safe_read_csv(dir + "investment_genmax_slpTh.csv", index_col=0, usecols=[0, 2])

    # Storage Cost (CHF/MWh):
    investment_emax_slp = import_and_keep_only_CH(dir + "investment_emax_slp.csv", usecols=[0, 2])
    investment_emax_slpTh = safe_read_csv(dir + "investment_emax_slpTh.csv", index_col=0, usecols=[0, 2])
    investment_fuel_storage_slp = safe_read_csv(dir + "investment_fuel_storage_slp.csv", index_col=0, usecols=[0, 2])

    # Create DataFrame to contain all the information: ------------------------------------------------------------

    # Define column names
    # columns = [
    #     "Existing Power (MW)", "Added Power (MW)", "New Power (MW)", 
    #     "Max New Power (MW)", "Existing Storage (MWh)", "Added Storage (MWh)", 
    #     "New Storage (MWh)", "Max New Storage (MWh)", "Investment Cost (CHF/MW)", 
    #     "Storage Cost (CHF/MWh)"
    # ]

    columns = [
        "Existing Power (MW)", "Added Power (MW)", "Max New Power (MW)",
        "Storage level (MWh)", "Max New Storage (MWh)",
        "Investment Cost (CHF/MW)", "Storage Cost (CHF/MWh)"
    ]

    # Create an empty DataFrame
    df = pd.DataFrame(columns=columns, index=[])
    df.index.name = "Technology"

    # List of DataFrames to extract indices from
    dataframes = [
        gen_max_infeedpower, gen_max, genTh_max, gen_max_limit, genTh_max_limit,
        fuel_storage_capacity_annual, gen_energy_max, gen_energyTh_max, 
        fuel_storage_investment_annual_limit, energy_max_limit, energyTh_max_limit,
        investment_genmax_slp, investment_genmax_slpTh, 
        investment_emax_slp, investment_emax_slpTh, investment_fuel_storage_slp
    ]

    # Collect all indices
    all_indices = set()  # Using a set to automatically remove duplicates
    for df_temp in dataframes:
        all_indices.update(df_temp.index)

    # Sort the indices alphabetically
    sorted_indices = sorted(all_indices)

    # Reindex the main DataFrame
    df = df.reindex(sorted_indices)

    # Fill the main DataFrame with values: --------------------------------------------------------------------------

    # Mapping of dataframes to their respective columns
    df_mapping = {
        "Existing Power (MW)": [gen_max_infeedpower],
        "Added Power (MW)": [gen_max, genTh_max],
        "New Power (MW)": [],
        "Max New Power (MW)": [gen_max_limit, genTh_max_limit],
        "Existing Storage (MWh)": [],
        "Added Storage (MWh)": [],
        "Storage level (MWh)": [fuel_storage_capacity_annual, gen_energy_max, gen_energyTh_max],
        "Max New Storage (MWh)": [fuel_storage_investment_annual_limit, energy_max_limit, energyTh_max_limit],
        "Investment Cost (CHF/MW)": [investment_genmax_slp, investment_genmax_slpTh],
        "Storage Cost (CHF/MWh)": [investment_emax_slp, investment_emax_slpTh, investment_fuel_storage_slp]
    }

    # # Populate the main DataFrame using df_mapping
    # for column, dataframes_list in df_mapping.items():
    #     for df_temp in dataframes_list:
    #         for index in df_temp.index:
    #             if index in df.index:
    #                 df.at[index, column] = df_temp.at[index, df_temp.columns[0]]

    # df["New Power (MW)"] = df["Existing Power (MW)"].fillna(0) + df["Added Power (MW)"].fillna(0)
    # #df.loc[(df["Existing Power (MW)"] == 0) & (df["Added Power (MW)"] == 0), "New Power (MW)"] = None

    for column in columns:

        for input in df_mapping[column]:

            for row in input.index:

                value = input.loc[row]

                first_val = get_first_value(value)

                df[column].loc[row] = first_val

    df.to_csv(dir + "investment_summary.csv")

def plants_at_swiss_nodes(dir):
    """The node of every plant and infeed plant, from the run's exported Map_node_plant.csv (each node, then its
    plants) and Map_infeedplant_node.csv, and the set of those at a node whose country is CH (Map_node_country.csv)."""
    def rows(name):
        with open(dir + name, newline="", encoding="utf-8") as f:
            return list(csv.reader(f))[1:]  # an empty export ("") has no row after the first

    node_of = {plant: row[0] for row in rows("Map_node_plant.csv") for plant in row[1:] if plant}
    infeed_node_of = {row[0]: row[1] for row in rows("Map_infeedplant_node.csv")}
    conflicting = sorted(p for p in infeed_node_of if p in node_of and node_of[p] != infeed_node_of[p])
    if conflicting:
        raise ValueError(f"Map_node_plant.csv and Map_infeedplant_node.csv place {conflicting} at different nodes")
    node_of.update(infeed_node_of)
    country = {row[0]: row[1] for row in rows("Map_node_country.csv")}
    without_country = sorted(set(node_of.values()) - set(country))
    if without_country:
        raise ValueError(f"Map_node_country.csv has no country for the nodes {without_country}")
    return node_of, {plant for plant, node in node_of.items() if country[node] == "CH"}


def get_first_value(value):
    if isinstance(value, pd.Series):
        return value.iloc[0]
    elif isinstance(value, pd.DataFrame):
        return value.iloc[0, 0]
    else:
        raise TypeError("Input must be a pandas Series or DataFrame")