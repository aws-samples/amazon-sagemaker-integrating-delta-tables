import argparse
import csv
import os
import shutil
import sys
import time
import boto3
import pandas as pd
from decimal import Decimal

# Import pyspark and build Spark session
from pyspark import SparkContext, SparkConf
from pyspark.sql import SparkSession, SQLContext
from feature_store_manager import FeatureStoreManager

# Defining some functions for efficiently ingesting into SageMaker Feature Store...
def remove_exponent(d):
    return d.quantize(Decimal(1)) if d == d.to_integral() else d.normalize()

def transform_row(columns, row) -> list:
    record = []
    for column in columns:
        if column != 'timestamp':
            try:
                feature = {'FeatureName': column, 'ValueAsString': str(remove_exponent(Decimal(str(row[column]))))}
            except:
                feature = {'FeatureName': column, 'ValueAsString': str(row[column])}
            # We can't ingest null value for a feature type into a feature group
            if str(row[column]) not in ['NaN', 'NA', 'None', 'nan', 'none']:
                record.append(feature)
    # Complete with EventTime feature
    timestamp = {'FeatureName': 'EventTime', 'ValueAsString': str(pd.to_datetime("now").timestamp())}
    record.append(timestamp)
    return record

def ingest_to_feature_store(fg, region, rows) -> None:
    session = boto3.session.Session(region_name=region)
    featurestore_runtime_client = session.client(service_name='sagemaker-featurestore-runtime')
    columns = rows.columns
    for index, row in rows.iterrows():
        record = transform_row(columns, row)
        #print(f'Putting record:{record}')        
        response = featurestore_runtime_client.put_record(FeatureGroupName=fg, Record=record)
        #print(f'Done with row:{index}')
        assert response['ResponseMetadata']['HTTPStatusCode'] == 200

def main():
    parser = argparse.ArgumentParser(description="app inputs and outputs")
    parser.add_argument("--region", type=str, help="AWS region")
    parser.add_argument("--table", type=str, help="Delta Table URL")
    parser.add_argument("--feature-group", type=str, help="Name of the Feature Group")
    parser.add_argument("--output-path", type=str, help="S3 prefix for storing resulting dataset")
    args = parser.parse_args()

    # Instantiate Spark via builder
    # Note: we use the `ContainerCredentialsProvider` to give us access to underlying IAM role permissions
    spark = (SparkSession
        .builder
        .appName("PySparkApp") 
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension") 
        .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog") 
        .config("fs.s3a.aws.credentials.provider",'com.amazonaws.auth.ContainerCredentialsProvider') 
        .getOrCreate())

    sc = spark.sparkContext
    print('Spark version: '+str(sc.version))
    
    s3a_delta_table_uri=args.table
    print(s3a_delta_table_uri)

    # Create SQL command inserting the S3 path location
    sql_cmd = f'SELECT * FROM delta.`{s3a_delta_table_uri}` ORDER BY timestamp'
    print(f'SQL command: {sql_cmd}')

    # Execute SQL command which returns dataframe
    sql_results = spark.sql(sql_cmd)
    print(type(sql_results))

    # ----------------
    # Transformations - Pandas code generated by sagemaker_datawrangler
    processed_features = sql_results.toPandas().copy(deep=True)

    # Code to Replace with new value for column: userID to resolve warning: Disguised missing values 
    generic_value = 'Other'
    processed_features['userID']=processed_features['userID'].replace('na', 'Other', regex=False)
    processed_features['userID']=processed_features['userID'].replace('nA', 'Other', regex=False)

    # Code to Drop column for column: ratingID to resolve warning: ID column 
    processed_features=processed_features.drop(columns=['ratingID'])
    
    # Complete with EventTime feature
    processed_features['EventTime']=str(pd.to_datetime('now').strftime('%Y-%m-%dT%H:%M:%SZ'))

    print(processed_features.head())

    # Capture resulting data frame in Spark
    #sqlContext = SQLContext(sc)
    #data=sqlContext.createDataFrame(processed_features)
    # ----------------
    columns = ['rowID', 'timestamp', 'userID', 'placeID', 'rating_overall', 'rating_food', 'rating_service', 'EventTime']
    df = spark.createDataFrame(processed_features).toDF(*columns)
    
    # Write processed data after transformations...
    processed_features_output_path = args.output_path + 'processed_features.csv'
    print("Saving processed features to {}".format(processed_features_output_path))
    df.write.csv(processed_features_output_path)

    # Ingesting the resulting data into our Feature Group...
    print(f"Ingesting processed features into Feature Group {args.feature_group}...")
    feature_store_manager = FeatureStoreManager()
    feature_store_manager.ingest_data(
        input_data_frame=df,
        feature_group_arn=f'{args.feature_group}',
        target_stores=['OfflineStore']
    )

    #ingest_to_feature_store(args.feature_group, args.region, processed_features)
    print("All done.")

if __name__ == "__main__":
    main()
