from IPython.display import display
from darts import concatenate
from darts import TimeSeries
import json
import numpy as np
import optuna
import os
import pandas as pd
import re
import time
import urllib.request

from darts.dataprocessing.transformers import Scaler
from darts.models import (BlockRNNModel, ExponentialSmoothing, LightGBMModel, NBEATSModel,
                          XGBModel)
from darts.models.forecasting.baselines import NaiveSeasonal
from darts.models.forecasting.torch_forecasting_model import _get_checkpoint_folder
from darts.utils.callbacks import TFMProgressBar
from darts.utils.timeseries_generation import datetime_attribute_timeseries as dt_attr
from darts.utils.utils import ModelMode, SeasonalityMode
import optuna
import pytorch_lightning as pl
from pytorch_lightning.callbacks import Callback, ModelCheckpoint
from pytorch_lightning import LightningModule
from pytorch_lightning import Trainer
import torch
from tqdm.notebook import tqdm

# metrics
from darts.metrics import mae, r2_score, rmse


def download_data(api_call: str, file_path: str, file_name: str):
    """
    Accepts an API call and downloads the data under the given file name at the file
    path location. 
    """
    try:
        response = urllib.request.urlopen(api_call)
        data = response.read()

        # decode data
        json_data = json.loads(data.decode('utf-8'))

        # save data to file
        with open(f'{file_path}{file_name}', 'w') as file:
          json.dump(json_data, file)
        print(f'Data successfully downloaded to {file_path}{file_name}.')
        
    except:
        print('Error: file not downloaded')

  
def df_from_json(file):
    """Reads in json weather data and returns a Pandas DataFrame."""
    with open(file) as f:
        contents = f.read()

    json_object = json.loads(contents)
    data = json_object['hourly']

    return pd.DataFrame(data)

def generate_df_summary(df, describe_only=False):
    """Accepts a pandas dataframe and prints out basic details about the data and dataframe structure."""
    
    object_columns = [col for col in df.columns if df[col].dtype == 'object']
    non_object_columns = [col for col in df.columns if df[col].dtype != 'object']
    
    print(f'Dataframe: {df.name}\n')

    if describe_only:
        print('------ Column Summaries: ------')
        if object_columns:
            display(df[object_columns].describe(include='all').transpose())
        if non_object_columns:
            display(df[non_object_columns].describe().transpose())
        print('\n')

    else:
        print(f'------ Head: ------')
        display(df.head())
        print('\n')
    
        print(f'------ Tail: ------')
        display(df.tail())
        print('\n')
        
        print('------ Column Summaries: ------')
        if object_columns:
            display(df[object_columns].describe(include='all').transpose())
        if non_object_columns:
            display(df[non_object_columns].describe().transpose())
        print('\n')

        print('------ Counts: ------\n')
        print(f'Rows: {df.shape[0]:,}') 
        print(f'Columns: {df.shape[1]:,}') 
        print(f'Duplicate Rows = {df.duplicated().sum()} | % of Total Rows = {df.duplicated().sum()/df.shape[0]:.1%}') 
        print('\n')

        print('------ Info: ------\n')
        display(df.info()) 
        print('\n')
        
        print('------ Missing Data Percentage: ------')
        display(df.isnull().sum()/len(df) * 100)   


def daily_aggregations(dataframe):
    """Aggregates the weather data at a daily level of granularity."""

    df_copy = dataframe.copy()
    df_copy['date'] = df_copy['time'].dt.normalize()
    df_copy = df_copy.set_index('date')

    daily_data = df_copy.loc[:, ['sunshine_s', 'precipitation', 'shortwave_radiation']].resample('D').sum()

    # convert to hourly values 
    daily_data['sunshine_hr'] = daily_data['sunshine_s'] / 3600

    # columns for min, mean, and max aggregations
    agg_cols = ['temp', 'humidity', 'dew_point', 'cloud_cover', 'wind_speed']

    # Compute min, mean, and max values

    # minimum aggregations
    mins = df_copy[agg_cols].resample('D').min()
    col_names_min = [f'min_{name}' for name in agg_cols]
    mins.columns = col_names_min

    # mean aggregations
    means = df_copy[agg_cols].resample('D').mean()
    col_names_mean = [f'mean_{name}' for name in agg_cols]
    means.columns = col_names_mean

    # max aggregations
    maxes = df_copy[agg_cols].resample('D').max()
    col_names_max = [f'max_{name}' for name in agg_cols]
    maxes.columns = col_names_max

    # merge the aggregated dataframes
    for df in [mins, means, maxes]:
        daily_data = pd.merge(daily_data, df, left_index=True, right_index=True)

    daily_data = daily_data.drop(columns='sunshine_s').round(3)

    # reorder the columns to display sunshine_hr first
    daily_data = daily_data.loc[:, ['sunshine_hr', 'shortwave_radiation', 'precipitation', 
                                    'min_temp','mean_temp', 'max_temp',
                                    'min_humidity', 'mean_humidity', 'max_humidity',
                                    'min_dew_point','mean_dew_point', 'max_dew_point',
                                    'min_cloud_cover',  'mean_cloud_cover', 'max_cloud_cover',
                                    'min_wind_speed', 'mean_wind_speed', 'max_wind_speed']]

    return daily_data

def daily_aggregations_v2(dataframe):
    """Aggregates the weather data at a daily level of granularity."""
    
    df_copy = dataframe.copy()
    df_copy['date'] = df_copy['time'].dt.normalize()
    df_copy = df_copy.set_index('date')
    
    stat_by_variable = {
        'sunshine_duration': 'sum',
        'humidity': 'mean',
    }
    
    # aggregate at the daily level 
    daily_data = df_copy.resample('D').agg(stat_by_variable) 
    df_temp = df_copy['temp'].resample('D').agg([np.min, np.mean, np.max])
    df_temp.columns = [f'temp_{col}' for col in df_temp.columns]
    
    # convert to hourly values 
    daily_data['sunshine_duration'] = daily_data['sunshine_duration'] / 3600 
    daily_data.rename(columns={'sunshine_duration': 'sunshine_hr',
                               'humidity': 'humidity_mean'}, inplace=True)
    
    daily_data = pd.merge(daily_data, df_temp, left_index=True, right_index=True)

    # reorder the columns to display sunshine_hr first
    reordered_columns = [col for col in daily_data if col != 'sunshine_hr']
    reordered_columns.insert(0, 'sunshine_hr')
    
    return daily_data[reordered_columns]

def get_season(month_day, data_type='string'):
    """
    Returns a season indicator based on a given month_day value.
    There is roughly a 1-3 day margin of error, given the 
    seasonal timeline in any given year.

    month_day: a value calculated based on month of year and day of month,
    such that January 1st = 101 and December 31st = 1231. 

    """
    try:

        if data_type == 'string':

            if ((month_day >= 320) and (month_day <= 619)):
                season = "Spring"
            elif ((month_day >= 620) and (month_day <= 921)):
                season = "Summer"
            elif ((month_day >= 922) and (month_day <= 1219)):
                season = "Fall"
            elif ((month_day >= 1220) or (month_day <= 319)):
                season = "Winter"
            else:
                raise IndexError("Invalid month_day Input")

        elif data_type == 'int':

            if ((month_day >= 320) and (month_day <= 619)):
                season = 1
            elif ((month_day >= 620) and (month_day <= 921)):
                season = 2
            elif ((month_day >= 922) and (month_day <= 1219)):
                season = 3
            elif ((month_day >= 1220) or (month_day <= 319)):
                season = 4
            else:
                raise IndexError("Invalid month_day Input")

        return season

    except:
        error_string = "Error: data_type selected should be 'int' or 'string' "
        return error_string
    


def adjust_outliers(data, columns, granularity='month'):
    """Caps outliers at +/- IQR*1.5 on the specified per-month or per-season basis."""
    
    df_clean = data.copy()
    global_outlier_count = 0
    
    
    for col in columns:
        outlier_count = 0
    
        if granularity == 'month':
            for month in set(df_clean['month'].unique()):
                Q1 = df_clean[df_clean['month'] == month][col].quantile(0.25)
                Q3 = df_clean[df_clean['month'] == month][col].quantile(0.75)
                IQR = Q3 - Q1
                lower = Q1 - 1.5*IQR
                upper = Q3 + 1.5*IQR

                outlier_count +=  len(df_clean[(df_clean['month'] == month) & (df_clean[col] > upper)]) + \
                len(df_clean[(df_clean['month'] == month) & (df_clean[col] < lower)])


                if outlier_count > 0:
                    df_clean[col] = np.where((df_clean['month'] == month) & (df_clean[col] > upper), upper, df_clean[col])
                    df_clean[col] = np.where((df_clean['month'] == month) & (df_clean[col] < lower), lower, df_clean[col])

        elif granularity == 'season':
            for season in set(df_clean['season_str'].unique()):
                Q1 = df_clean[df_clean['season_str'] == season][col].quantile(0.25)
                Q3 = df_clean[df_clean['season_str'] == season][col].quantile(0.75)
                IQR = Q3 - Q1
                lower = Q1 - 1.5*IQR
                upper = Q3 + 1.5*IQR


                outlier_count +=  len(df_clean[(df_clean['season_str'] == season) & (df_clean[col] > upper)]) + \
                len(df_clean[(df_clean['season_str'] == season) & (df_clean[col] < lower)])

                if outlier_count > 0:
                    df_clean[col] = np.where((df_clean['season_str'] == season) & (df_clean[col] > upper), upper, df_clean[col])
                    df_clean[col] = np.where((df_clean['season_str'] == season) & (df_clean[col] < lower), lower, df_clean[col])

        else:
            print('Invalid granularity specified. Please indicate "month" or "season".')
            return None


        global_outlier_count += outlier_count
        print(f'Total outliers adjusted in the {col} column: {outlier_count:,}')
        print(f'Percent of total rows: {outlier_count/len(df_clean):.2%}')
        print('\n')

    return df_clean

def create_timeseries(df, col):
  """Creates a TimeSeries object for the given column with data type float 32 for quicker training/processing."""
  df = df.copy().reset_index()
  return TimeSeries.from_dataframe(df[['date', col]], 'date', col).astype(np.float32) 

def get_covariate_ts(df):
    df = df.copy().reset_index()
    
    """Returns timeseries objects for the combined covariates. """
    
    time_series = {
        'covariates': {}
        }
    
    for col in df.columns[2:]:
        time_series['covariates'][col] = create_timeseries(df, col)

    # create stacked timeseries for the covariates
    covariates_ts = concatenate([ts for ts in time_series['covariates'].values()],
                              axis=1)

    return covariates_ts

def get_clean_df(df, agg_cols):
    """Aggregates data and removes outliers on a per-month basis. """
    df = df.copy()
    df['time'] = pd.to_datetime(df['time'])
    
    # create daily aggregations 
    df = daily_aggregations_v2(df, agg_cols)
    df.drop(['humidity_min', 'humidity_max'], axis=1, inplace=True)
    df['temp_range'] = df['temp_max'] - df['temp_min']
    df['month'] = df.index.month
    
    month_label_abbr = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug',
               'Sep', 'Oct', 'Nov', 'Dec']
    
    cols_to_adjust = list(df.columns[:-1])
    
    df_clean = adjust_outliers(df, columns=cols_to_adjust, granularity='month')
    df_clean.drop('month', axis=1, inplace=True)

    return df_clean
    
def post_hyperparam_results(results, file, mode='a'):
    """Records the best hyper parameter search results to a .json file."""

    try:
        with open(file, mode) as output_file:
            json.dump(results, output_file)
            print(f'Successfully posted results to {file}')
    except Exception as e:
        print('Unable to save results to file')
        print(e)

def read_json_file(file, output_type='dict'):
    """Reads in json file and returns a dictionary or pandas dataframe."""

    with open(file) as json_file:
        if output_type == 'dict':
            data = json.load(json_file)
        elif output_type == 'df':
            data = pd.read_json(json_file)

    return data 

def print_callback(study, trial):
  print(f"Current value: {trial.value}, Current params: {trial.params}")
  print(f"Current Best value: {study.best_value}, Best params: {study.best_trial.params}")

def hyperparameter_search(objective, n_trials, model_name):
    """
    Completes an Optuna hyperparameter search and returns the results 
    after n_trials. 
     """
    start_time = time.perf_counter()
    study = optuna.create_study(direction='minimize')

    # limit number of trials
    study.optimize(objective, n_trials=n_trials, callbacks=[print_callback])

    end_time = time.perf_counter()
    operation_runtime = (end_time - start_time)/60

    #print the best value and best hyperparameters:
    print(f'Best value: {study.best_value:.4f}\nBest parameters: {study.best_trial.params}')

    print(f'Operation runtime: {operation_runtime:.2f} minutes')

    results = {model_name: {
        'batch_size': study.best_trial.params['batch_size'],
        'n_epochs': study.best_trial.params['n_epochs'],
        'num_blocks': study.best_trial.params['num_blocks'],
        'num_layers': study.best_trial.params['num_layers'],
        'dropout': study.best_trial.params['dropout'],
        'activation': study.best_trial.params['activation'],
        'lr': study.best_trial.params['lr'],
        'rmse': study.best_value,
        'hyp_search_runtime': operation_runtime
        }
    }

    return results

def generate_cutoff_date(start_date, end_date, seed, n=1, replace=False):
    """Generates a random date from a given range (start_date to end_date) for the training cutoff."""
    dates = pd.date_range(start_date, end_date).to_series()

    dates = dates.sample(n, replace=replace, random_state=seed)

    date = dates[0].strftime('%Y-%m-%d')

    year, month, day = [int(x) for x in date.split('-')]

    cutoff_date = f'{year}-{month}-{day}'

    return cutoff_date

def get_model(model_name, fh, hyp_params, model_file_path, seed, version=None):

    """Returns an unfitted model based on the given name, forecast horizon,
     hyperparameter dictionary, and model version (in the case of N-BEATS)."""

    if model_name not in ['naive_seasonal', 'exponential_smoothing']:
        hyp = hyp_params[model_name]

    if model_name in ['lstm', 'gru', 'nbeats', 'nhits']:

        torch.manual_seed(seed)

        model_name_fh = f'{model_name}_{fh}'

        checkpoint_name = f'{model_name}_ckpt'

        checkpoint_callback = ModelCheckpoint(
            monitor='train_torchmetrics',
            filename='best-{epoch}-{MeanAbsoluteError:.2f}',
            dirpath= _get_checkpoint_folder(
                work_dir = os.path.join(os.getcwd(), "darts_logs"),
                model_name = model_name_fh,
            )
        )

        # detect whether a GPU is available
        # print(f'Available GPU: {torch.cuda.is_available()}\n')
        if torch.cuda.is_available():
            pl_trainer_kwargs = {
                'accelerator': 'gpu',
                'callbacks': [checkpoint_callback],
            }
        else:
            pl_trainer_kwargs = {'callbacks': [checkpoint_callback]}


        if model_name in ['lstm', 'gru']:

            model = BlockRNNModel(
                model = model_name.upper(),
                input_chunk_length = hyp[fh]['parameters']['input_chunk_length'],
                output_chunk_length = fh,
                batch_size =  hyp[fh]['parameters']['batch_size'],
                n_epochs = hyp[fh]['parameters']['n_epochs'],
                hidden_dim = hyp[fh]['parameters']['hidden_dim'],
                n_rnn_layers = hyp[fh]['parameters']['n_rnn_layers'],
                dropout = hyp[fh]['parameters']['dropout'],
                pl_trainer_kwargs = pl_trainer_kwargs,
                optimizer_kwargs = {'lr': hyp[fh]['parameters']['lr'] },
                log_tensorboard=True,
                model_name = model_name_fh,
                save_checkpoints=True,
                force_reset=True

            )

        elif model_name == 'nbeats':

            model = NBEATSModel(
                random_state=1,
                input_chunk_length = hyp[version][fh]['parameters']['input_chunk_length'],
                output_chunk_length = fh,
                batch_size = hyp[version][fh]['parameters']['batch_size'],
                n_epochs = hyp[version][fh]['parameters']['n_epochs'],
                dropout = hyp[version][fh]['parameters']['dropout'],
                activation =  hyp[version][fh]['parameters']['activation'],
                generic_architecture=True if version == 'generic' else False,
                pl_trainer_kwargs = pl_trainer_kwargs,
                optimizer_kwargs = {'lr': hyp[version][fh]['parameters']['lr'] },
                log_tensorboard=True,
                model_name = model_name_fh,
                save_checkpoints=True,
                force_reset=True
            )

        model.save(f'{model_file_path}{model_name}_fh{fh}.pt')

    else:

        if model_name == 'naive_seasonal':
            model = NaiveSeasonal(K=365)

        elif model_name == 'exponential_smoothing':
            model = ExponentialSmoothing(trend=ModelMode.ADDITIVE,
                                    seasonal=SeasonalityMode.ADDITIVE,
                                    seasonal_periods=365)

        elif model_name == 'xgboost':
            model = XGBModel(
                lags = hyp[fh]['parameters']['lags'],
                lags_past_covariates = hyp[fh]['parameters']['lags_past_covariates'],
                output_chunk_length = fh
            )

        elif model_name == 'lgbm':
            model = LightGBMModel(
                lags = hyp[fh]['parameters']['lags'],
                lags_past_covariates = hyp[fh]['parameters']['lags_past_covariates'],
                output_chunk_length = fh,
                verbose=-1
            )

        model.save(f'{model_file_path}{model_name}_fh{fh}.pkl')


    if model_name == 'nbeats':
            model_name_unique = f'{model_name}_{version}_fh{fh}'
    else:
        model_name_unique = f'{model_name}_fh{fh}'


    return model, model_name_unique

def get_reformatted_hyperparams(hyp_dict, forecast_horizons):

    """
    Accepts a dictionary with the results of hyperparameter tuning and returns a reformatted version
    for the machine learning experiments.
    """

    new_hyp_dict = {

        'gru': {fh: {} for fh in forecast_horizons},
        'lgbm': {fh: {} for fh in forecast_horizons},
        'lstm': {fh: {} for fh in forecast_horizons},
        'nbeats': {
            'generic': {fh: {} for fh in forecast_horizons},
            'interpretable': {fh: {} for fh in forecast_horizons}
        },
        'xgboost': {fh: {} for fh in forecast_horizons}
    }


    for hyp_name, values in hyp_dict.items():

        if len(hyp_name.split('_')) == 3:

            model_name_main, version, fh_str = hyp_name.split('_')
            fh = int(re.search(r'(?:h)(.*)', fh_str).group(1))

            new_hyp_dict[model_name_main][version][fh] = {
                'parameters': values['best_parameters'],
                'training_rmse': values['best_rmse'],
                'hyp_search_time': values['hyperparam_search_time']
            }

        else:

            model_name_main, fh_str = hyp_name.split('_')
            fh = int(re.search(r'(?:h)(.*)', fh_str).group(1))

            new_hyp_dict[model_name_main][fh] = {
                'parameters': values['best_parameters'],
                'training_rmse': values['best_rmse'],
                'hyp_search_time': values['hyperparam_search_time']
            }

    return new_hyp_dict


def run_experiment(model, model_names, hyper_parameters, cutoff_date, fh,
                   df_outliers, df_clean, outliers, global_results, 
                   model_file_path, results_path_file):
    """Runs an experiment and saves the results to a file."""

    model_name = model_names[0]
    model_name_proper = model_names[1]
    model_name_unique = model_names[2]

    # get training and testing data (only complete past covariate min-max scaling for non-N-BEATS models)
    if model_name == 'nbeats':
        target_train, target_test, past_covariates = train_test_split(df_outliers, df_clean, cutoff_date, outliers=outliers, nbeats=True)
    else:
        target_train, target_test, past_covariates_trf = train_test_split(df_outliers, df_clean, cutoff_date, outliers=outliers)

    print(f'\nRunning {model_name_proper} Experiments - Forecast Horizon: {fh} | Outliers: {outliers}...\n')

    start_time = time.perf_counter()

    if model_name in ['naive_seasonal', 'exponential_smoothing']:
        model.fit(series=target_train)

    elif model_name == 'nbeats':
        model.fit(series=target_train,
                past_covariates=past_covariates,
                verbose=False)
        model.save(f'{model_file_path}{model_name}_fh{fh}_fitted.pt')

    else:
        model.fit(series=target_train,
                past_covariates=past_covariates_trf)
        model.save(f'{model_file_path}{model_name}_fh{fh}_fitted.pkl')

    y_pred = model.predict(n=fh)
    rmse_score = rmse(y_pred, target_test[:fh])
    mae_score = mae(y_pred, target_test[:fh])

    end_time = time.perf_counter()
    training_time = (end_time - start_time) / 60


    if model_name not in ['naive_seasonal', 'exponential_smoothing']:
        hyp_search_time = hyper_parameters[model_name_unique]['hyperparam_search_time']
        best_val_rmse = hyper_parameters[model_name_unique]['best_rmse']
    else:
        hyp_search_time = np.nan
        best_val_rmse = np.nan


    total_time = round(training_time + hyp_search_time, 2)

    # Record results
    global_results['model_name_proper'].append(model_names[1])
    global_results['model_name_unique'].append(model_name_unique)
    global_results['outlier_indicator'].append(outliers)
    global_results['forecast_horizon'].append(fh)
    global_results['rmse'].append(rmse_score)
    global_results['mae'].append(mae_score)
    global_results['best_val_rmse'].append(best_val_rmse)
    global_results['training_time'].append(training_time)
    global_results['hyp_search_time'].append(hyp_search_time)
    global_results['total_time'].append(total_time)

    if model_name == 'nbeats': # breaking up the N-BEATS experiements into False/True re: Outliers purely to avoid Colab execution timeout and progress/data loss 
        file_name = f'{results_path_file}{model_name}_outliers-{outliers}_experiment_results.csv'
    else:
        file_name = f'{results_path_file}{model_name}_experiment_results.csv'
    pd.DataFrame(global_results).to_csv(file_name)


def train_test_split(df_outliers, df_clean, cutoff_date, outliers=False, nbeats=False):

    if outliers==False:

        target = create_timeseries(df_clean, 'sunshine_hr')

        # create past covariates as stacked timeseries of exogenous variables
        past_covariates = get_covariate_ts(df_clean)

        # create training and testing datasets
        training_cutoff = pd.Timestamp(cutoff_date)

        target_train, target_test = target.split_after(training_cutoff)
        covariates_train, covariates_test = past_covariates.split_after(training_cutoff)

        covariate_scaler = Scaler()
        covariate_scaler.fit(covariates_train)
        past_covariates_trf = covariate_scaler.transform(past_covariates)

    elif outliers == True:

        target = create_timeseries(df_outliers, 'sunshine_hr')

        # create past covariates as stacked timeseries of exogenous variables
        past_covariates = get_covariate_ts(df_outliers)

        # create training and testing datasets
        training_cutoff = pd.Timestamp(cutoff_date)

        target_train, target_test = target.split_after(training_cutoff)
        covariates_train, covariates_test = past_covariates.split_after(training_cutoff)

        covariate_scaler = Scaler()
        covariate_scaler.fit(covariates_train)
        past_covariates_trf = covariate_scaler.transform(past_covariates)


    if nbeats:
        return target_train, target_test, past_covariates
    else:
        return target_train, target_test, past_covariates_trf


def highlight_maxormin(df, max=True, starting_col_idx=0):
    """"Highlights the minimum or maximum value in each row within a given df."""
    df_styled = df.style.format("{:.1f}").hide()

    for row in df.index:
        if max:
            col = df.loc[row][starting_col_idx:].idxmax()
        else:
            col = df.loc[row][starting_col_idx:].idxmin()

        # redo formatting for a specific cell
        df_styled = df_styled.format(lambda x: "\\textbf{" + f'{x:.2f}' + "}", subset=(row, col))


    return df_styled
