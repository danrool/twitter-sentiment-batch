#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""A word-counting workflow."""

# pytype: skip-file

from __future__ import absolute_import

import argparse
import logging
import re
import os
import csv
import random
import json

from past.builtins import unicode

import apache_beam as beam
from apache_beam.io import ReadFromText
from apache_beam.io import WriteToText
from apache_beam.coders.coders import Coder
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions, DirectOptions
from apache_beam.options.pipeline_options import StandardOptions
from apache_beam.options.pipeline_options import GoogleCloudOptions

import nltk
from nltk.corpus import stopwords
from nltk.stem import SnowballStemmer

nltk.download("stopwords")

# CLEANING
STOP_WORDS = stopwords.words("english")
STEMMER = SnowballStemmer("english")
TEXT_CLEANING_RE = "@\S+|https?:\S+|http?:\S|[^A-Za-z0-9]+"


class ExtractColumnsDoFn(beam.DoFn):
    def process(self, element):
        record = json.loads(element)
        content = record.get("content", "")
        sentiment_label = record.get("annotation", {}).get("label", [None])[0]
        yield content, sentiment_label


class PreprocessColumnsTrainFn(beam.DoFn):
    def process_sentiment(self, sentiment):
        sentiment = int(sentiment)
        if sentiment == 1:
            return "POSITIVE"
        else:
            return "NEGATIVE"

    def process_text(self, text):
        # Remove link,user and special characters
        stem = False
        text = re.sub(TEXT_CLEANING_RE, " ", str(text).lower()).strip()
        tokens = []
        for token in text.split():
            if token not in STOP_WORDS:
                if stem:
                    tokens.append(STEMMER.stem(token))
                else:
                    tokens.append(token)
        return " ".join(tokens)

    def process(self, element):
        processed_text = self.process_text(element[0])
        processed_sentiment = self.process_sentiment(element[1])
        yield f"{processed_text}, {processed_sentiment}"


class CustomCoder(Coder):
    """A custom coder used for reading and writing strings"""

    def __init__(self, encoding: str):
        # latin-1
        # iso-8859-1
        self.enconding = encoding

    def encode(self, value):
        return value.encode(self.enconding)

    def decode(self, value):
        return value.decode(self.enconding)

    def is_deterministic(self):
        return True


def run(argv=None, save_main_session=True):

    """Main entry point; defines and runs the wordcount pipeline."""

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--work-dir", dest="work_dir", required=True, help="Working directory",
    )

    parser.add_argument(
        "--input", dest="input", required=True, help="Input dataset in work dir",
    )
    parser.add_argument(
        "--output",
        dest="output",
        required=True,
        help="Output path to store transformed data in work dir",
    )
    parser.add_argument(
        "--mode",
        dest="mode",
        required=True,
        choices=["train", "test"],
        help="Type of output to store transformed data",
    )

    #-----------------------
    # GCP
    #
    parser.add_argument(
        '--runner',
        dest='runner',
        required=False,
        default='DirectRunner', # Default DirectRunner to local environments
        help='Runner to use: DirectRunner (default) or DataflowRunner'
    )

    # Argumentos  DataflowRunner
    parser.add_argument(
        '--project',
        dest='project',
        required=False,
        help='The GCP project ID'
    )
    parser.add_argument(
        '--temp_location',
        dest='temp_location',
        required=False,
        help='Temporary location in GCS for Dataflow'
    )
    parser.add_argument(
        '--region',
        dest='region',
        required=False,
        help='Region for DataflowRunner'
    )


    known_args, pipeline_args = parser.parse_known_args(argv)

    # We use the save_main_session option because one or more DoFn's in this
    # workflow rely on global context (e.g., a module imported at module level).
    pipeline_options = PipelineOptions(pipeline_args)
    pipeline_options.view_as(SetupOptions).save_main_session = save_main_session
    pipeline_options.view_as(DirectOptions).direct_num_workers = 0

    ## GCP
    if known_args.runner == 'DataflowRunner':
        # Asegura que se proporcionan las opciones necesarias para DataflowRunner
        if not known_args.project or not known_args.temp_location or not known_args.region:
            parser.error('--project, --temp_location, and --region are required for DataflowRunner')
       
        # Configura opciones especificas para DataflowRunner
        pipeline_options.view_as(SetupOptions).save_main_session = save_main_session
        standard_options = pipeline_options.view_as(StandardOptions)
        standard_options.runner = known_args.runner
        google_cloud_options = pipeline_options.view_as(GoogleCloudOptions)
        google_cloud_options.project = known_args.project
        google_cloud_options.temp_location = known_args.temp_location
        google_cloud_options.region = known_args.region
    else:
        pipeline_options.view_as(SetupOptions).save_main_session = save_main_session
        pipeline_options.view_as(DirectOptions).direct_num_workers = 0
    

    

    # The pipeline will be run on exiting the with block.
    with beam.Pipeline(options=pipeline_options) as p:

        # Read the text file[pattern] into a PCollection.
        raw_data = p | "ReadTwitterData" >> ReadFromText(
            known_args.input, coder=CustomCoder("latin-1")
        )

        if known_args.mode == "train":

            transformed_data = (
                raw_data
                | "ExtractColumns" >> beam.ParDo(ExtractColumnsDoFn())
                | "Preprocess" >> beam.ParDo(PreprocessColumnsTrainFn())
            )

            eval_percent = 20
            assert 0 < eval_percent < 100, "eval_percent must in the range (0-100)"
            train_dataset, eval_dataset = (
                transformed_data
                | "Split dataset"
                >> beam.Partition(
                    lambda elem, _: int(random.uniform(0, 100) < eval_percent), 2
                )
            )

            train_dataset | "TrainWriteToCSV" >> WriteToText(
                os.path.join(known_args.output, "train", "part")
            )
            eval_dataset | "EvalWriteToCSV" >> WriteToText(
                os.path.join(known_args.output, "eval", "part")
            )

        else:
            transformed_data = (
                raw_data
                | "ExtractColumns" >> beam.ParDo(ExtractColumnsDoFn())
                | "Preprocess" >> beam.Map(lambda x: f'"{x[0]}"')
            )

            transformed_data | "TestWriteToCSV" >> WriteToText(
                os.path.join(known_args.output, "test", "part")
            )


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    run()
