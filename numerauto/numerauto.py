"""
Main Numerauto module
"""

import time
import pickle
import datetime
import signal
import sys
import os
import shutil
from pathlib import Path
import logging

import requests
import pytz
import dateutil

from .robust_numerapi import RobustNumerAPI
from .utils import check_dataset


logger = logging.getLogger(__name__)


class Numerauto:
    """
    Numerai daemon.

    The Numerauto class implements a daemon that automatically detects the
    start of new Numerai rounds. Custom event handlers can be added to the
    Numerauto instance to add custom code that trains and applies models to new
    data, and uploads predictions for each round.

    See numerauto.handlers for some basic event handlers.

    Attributes:
        tournament_id: Numerai tournament id for which this instance will download data.
        data_directory: Directory where to store data.
        napi: A robust version of NumerAPI (note that no API keys are supplied)
        exit_requested: Flag that signals the daemon to stop looping if set to True.
        event_handlers: List of event handlers that are bound to this instance.
        dataset_path: Path of the last downloaded dataset.
        persistent_state: Internal storage of the current state of the daemon.
        round_number: Current round number.
    """

    def __init__(self, tournament_id=1, data_directory=Path('./data')):
        """
        Creates a Numerauto instance.

        Args:
            tournament_id: Numerai tournament id for which this instance will download data.
            data_directory: Directory where to store data (default: ./data)
        """
        self.tournament_id = tournament_id
        self.data_directory = Path(data_directory)
        self.napi = RobustNumerAPI(verbosity='warning', show_progress_bars=False)
        self.exit_requested = False
        self.event_handlers = []
        self.dataset_path = None
        self.persistent_state = None
        self.round_number = None

    def add_event_handler(self, handler):
        """
        Add an event handler to this instance.

        Args:
            handler: Event handler to add.
        """

        self.event_handlers.append(handler)
        handler.numerauto = self

    def remove_event_handler(self, handler_name):
        """
        Remove an event handler from this instance by its name.

        Args:
            handler_name: Name of the event handler to remove.
        """

        for h in self.event_handlers:
            if h.name == handler_name:
                h.numerauto = None

        self.event_handlers = [h for h in self.event_handlers if h.name != handler_name]

    def on_start(self):
        """ Internal event on daemon start """

        logger.debug('on_start')
        for h in self.event_handlers:
            h.on_start()

    def on_shutdown(self):
        """ Internal event on daemon shutdown """

        logger.debug('on_shutdown')
        for h in self.event_handlers:
            h.on_shutdown()

    def on_round_begin(self, round_number):
        """ Internal event on round start """

        logger.debug('on_round_begin(%d)', round_number)
        for h in self.event_handlers:
            h.on_round_begin(round_number)

    def on_new_training_data(self, round_number):
        """ Internal event on detection of new training data """

        logger.debug('on_new_training_data(%d)', round_number)
        for h in self.event_handlers:
            h.on_new_training_data(round_number)

    def on_new_tournament_data(self, round_number):
        """ Internal event on detection of new tournament data """

        logger.debug('on_new_tournament_data(%d)', round_number)
        for h in self.event_handlers:
            h.on_new_tournament_data(round_number)

    def check_new_training_data(self, round_number):
        """
        Internal function to check if the newly downloaded dataset contains
        new training data.
        """

        logger.debug('check_new_training_data(%d)', round_number)
        if self.persistent_state['last_round_trained'] is None:
            logger.info('check_new_training_data: last_round_trained not set, '
                        'treating training data as new')
            return True

        filename_old = self.get_dataset_path(self.persistent_state['last_round_trained']) / 'numerai_training_data.csv'
        filename_new = self.get_dataset_path(round_number) / 'numerai_training_data.csv'
        return check_dataset(filename_old, filename_new)

    def on_round_begin_internal(self, round_number):
        """ Internal event on round start """

        logger.debug('on_round_begin_internal(%d)', round_number)
        self.on_round_begin(round_number)

        # Check if training is needed, if so call on_new_training_data
        if self.check_new_training_data(round_number):
            # Signal new training data
            self.on_new_training_data(round_number)
            self.persistent_state['last_round_trained'] = round_number

            # Immediately save state to prevent retraining if other event handlers fail
            self.save_state()

        # Signal new tournament data
        self.on_new_tournament_data(round_number)


    def interruptible_wait(self, seconds):
        """
        Helper function that waits for a given number of seconds while checking
        the exit_requested attribute. If exit_requested is set to True, this
        function will return.

        Args:
            seconds: Number of seconds to wait.
        """

        logger.debug('interruptible_wait(%d)', seconds)
        dt_start = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)
        dt_now = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)

        while (dt_now - dt_start).total_seconds() < seconds:
            time.sleep(1)
            dt_now = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)

            if self.exit_requested:
                logger.debug('interruptible_wait interrupted after %d seconds',
                             (dt_now - dt_start).total_seconds())
                return


    def wait_till_next_round(self):
        """
        Wait until a new Numerai round is detected. Will wait until 5 minutes
        before the closing time of the current round, as reported by the
        Numerai API. Then the current round number is requested every minute
        until a new round number is received.

        Returns:
            Dictionary with the new round information.
        """

        logger.debug('wait_till_next_round')

        round_info = self.napi.get_current_round_details(tournament=self.tournament_id)
        dt_round_close = dateutil.parser.parse(round_info['closeTime'])

        new_round_info = self.napi.get_current_round_details(tournament=self.tournament_id)

        while new_round_info is None and not self.exit_requested:
            logger.warning('wait_till_next_round: API did not respond to round info request')
            self.interruptible_wait(60)
            new_round_info = self.napi.get_current_round_details(tournament=self.tournament_id)

        dt_now = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)
        logger.info('Waiting for round %d. Time to next round: %.1f hours',
                    self.persistent_state['last_round_processed'] + 1,
                    (dt_round_close - dt_now).total_seconds() / 3600)

        # Loop until the API reports a new round number
        while new_round_info['number'] == round_info['number'] and not self.exit_requested:
            dt_now = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)
            seconds_wait = (dt_round_close - dt_now).total_seconds() + 5

            if seconds_wait > 360:
                # Wait till 5 minutes before round start
                self.interruptible_wait(seconds_wait - 360)
            else:
                # Then query round information every minute until round has started
                self.interruptible_wait(min(seconds_wait, 60))

            if self.exit_requested:
                return None

            new_round_info = self.napi.get_current_round_details(tournament=self.tournament_id)
            dt_now = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)
            logger.info('Periodic check before planned round start. Current '
                        'round: %d. Time to next round: %.1f minutes',
                        new_round_info['number'],
                        (dt_round_close - dt_now).total_seconds() / 60)

            while new_round_info is None and not self.exit_requested:
                logger.warning('wait_till_next_round: API did not respond to round info request')
                self.interruptible_wait(60)
                new_round_info = self.napi.get_current_round_details(tournament=self.tournament_id)

        return new_round_info


    def download_dataset(self):
        """
        Downloads the current dataset to the directory specified in the
        data_directory attribute.

        Returns:
            Dataset path
        """

        logger.info('Downloading dataset')
        self.dataset_path = self.napi.download_current_dataset(dest_path=self.data_directory,
                                                               unzip=True,
                                                               tournament=self.tournament_id)
        return self.dataset_path


    def get_dataset_path(self, round_number):
        """
        Get the base path for the data for a given round number.

        Args:
            round_number: Number of the round for which the path is requested.

        Returns:
            pathlib Path for the dataset of the requested round.
        """

        return self.data_directory / 'numerai_dataset_{}'.format(round_number)


    def download_and_check(self):
        """
        Download a new dataset and check whether it contains new tournament
        data.

        Returns:
            True if the new dataset was validated as new data. False otherwise.
        """

        logger.debug('download_and_check')
        try:
            self.download_dataset()

            filename_old = self.get_dataset_path(self.round_number - 1) / 'numerai_tournament_data.csv'
            filename_new = self.get_dataset_path(self.round_number) / 'numerai_tournament_data.csv'

            valid = check_dataset(filename_old, filename_new, data_type='live')
        except requests.RequestException:
            import traceback
            msg = traceback.format_exc()
            logging.warning('Request exception: %s', msg)

            valid = False

        return valid


    def run_new_round(self):
        """
        Internal function that downloads and verifies a new dataset and calls
        the internal event handlers.
        """

        logger.debug('run_new_round')

        # Download data. If data is not valid, wait 10 minutes and try again.
        valid = self.download_and_check()

        while not valid and not self.exit_requested:
            logger.info('run_new_round: New dataset is not valid, retrying in 10 minutes')

            # Remove downloaded and unzipped files
            if hasattr(self, 'dataset_path'):
                os.remove(self.dataset_path)
                if os.path.isdir(self.dataset_path[:-4]):
                    shutil.rmtree(self.dataset_path[:-4])

            self.interruptible_wait(600)
            if self.exit_requested:
                return

            valid = self.download_and_check()

        # Call round begin event
        self.on_round_begin_internal(self.round_number)

        # Save current round as the last round processed
        self.persistent_state['last_round_processed'] = self.round_number

        # Save persistent state (in case of any crash)
        self.save_state()


    def load_state(self):
        """ Load the internal state from file using pickle. """

        logger.debug('load_state')

        # Try loading the state from file
        try:
            with open('state.pickle', 'rb') as fp:
                self.persistent_state = pickle.load(fp)
        except FileNotFoundError:
            self.persistent_state = {}
        except EOFError:
            self.persistent_state = {}

        # Set last round processed and trained if it does not exist
        if 'last_round_processed' not in self.persistent_state:
            self.persistent_state['last_round_processed'] = None

        if 'last_round_trained' not in self.persistent_state:
            self.persistent_state['last_round_trained'] = None

        logger.debug('load_state: last_round_processed = %s',
                     self.persistent_state['last_round_processed'])
        logger.debug('load_state: last_round_trained = %s',
                     self.persistent_state['last_round_trained'])


    def save_state(self):
        """ Save the internal state to file using pickle. """

        logger.debug('save_state')
        logger.debug('save_state: last_round_processed = %s',
                     self.persistent_state['last_round_processed'])
        logger.debug('save_state: last_round_trained = %s',
                     self.persistent_state['last_round_trained'])

        with open('state.pickle', 'wb') as fp:
            pickle.dump(self.persistent_state, fp)


    def signalHandler(self, sig, frame):
        """ SIGINT/SIGTERM handler """

        logger.info('Interrupt received!')
        self.exit_requested = True


    # Run Numerauto in daemon mode
    def run(self):
        """
        Start the Numerauto daemon. Will process Numerai rounds until
        interrupted.
        """
        logger.debug('run')

        # Set up signal handlers to gracefully exit
        signal.signal(signal.SIGINT, self.signalHandler)
        signal.signal(signal.SIGTERM, self.signalHandler)

        # Load internal state
        self.load_state()

        # Trigger start event
        self.on_start()

        self.round_number = self.napi.get_current_round()
        if (self.persistent_state['last_round_processed'] is None or
                self.persistent_state['last_round_trained'] is None or
                self.round_number > self.persistent_state['last_round_processed']):
            logger.info('Current round (%d) does not appear to be processed',
                        self.round_number)
            self.run_new_round()

        logger.info('Entering daemon loop')

        while not self.exit_requested:
            # Wait till next round starts
            round_info = self.wait_till_next_round()
            if self.exit_requested:
                break

            self.round_number = round_info['number']
            self.run_new_round()

        logger.info('Exiting daemon loop')

        # Trigger shutdown event
        self.on_shutdown()

        # Save internal state
        self.save_state()


# Make this file runnable as a standalone test without event handlers.
# Will download data and wait for each round.
if __name__ == "__main__":
    log_format = "%(asctime)s [%(levelname)8s] %(name)s: %(message)s"
    logging.basicConfig(format=log_format, level=logging.DEBUG,
                        handlers=[logging.FileHandler('debug.log'),
                                  logging.StreamHandler(sys.stdout)])
    try:
        Numerauto().run()
    except Exception as e:
        logging.exception(e)