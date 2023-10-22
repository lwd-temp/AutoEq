# -*- coding: utf-8 -*-

import os
import sys
from pathlib import Path
from measurements.prompt_list_item import PromptListItem
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent))
import pandas as pd
from rapidfuzz import fuzz
import requests
import shutil
import re
from bs4 import BeautifulSoup
import ipywidgets as widgets
from selenium.webdriver.common.by import By
from autoeq.utils import is_file_name_allowed
from abc import ABC, abstractmethod
from time import sleep, time
from measurements.name_index import NameIndex, NameItem
from measurements.manufacturer_index import ManufacturerIndex
from measurements.name_prompt import NamePrompt

DIR_PATH = os.path.abspath(os.path.join(__file__, os.pardir))


class UnknownManufacturerError(Exception):
    pass


class Crawler(ABC):
    """Crawler base class for crawling a frequency response measurement database index, resolving names, downloading
    data and processing the data to save in a standard AutoEq format in a correct target location in measurements
    directory.

    1. The database index is crawled and URLs for the data files are created
    2. Source names, AutoEq names, headphone forms and measurement rigs are attempted to be resolved
    3. A set of user prompts are created for IPyWidgets based UI
    4. User goes through the prompts
        a. Crawl index items and name index are updated
        b. An extra lazy resolution attempt is made when a prompt is opened, if this results in full resolution,
           the prompt is automatically fulfilled. This helps avoiding prompting different measurements of the same
           headphone model (reseats, left / right, samples)
    5. All items are processed (file download, PDF parsing, image digitization, JSON / CSV parsing, ...)
        a. All measurements of the same headphone model are processed together, frequency responses are averaged

    """
    def __init__(self, driver=None, delete_existing_on_prompt=True, redownload=False):
        self._start_time = time()
        self._timings = {}
        # Name resolutions
        self.name_index = self.read_name_index()
        self.manufacturers = ManufacturerIndex()
        self.name_proposals = None
        # Crawler
        self.driver = driver
        self.crawl_index = None
        self.delete_existing_on_prompt = delete_existing_on_prompt
        self.redownload = redownload
        # UI
        self.prompts = []
        self.prompt_list = None
        self.active_list_item = None
        self.widget = widgets.HBox([])

    # Name resolution methods

    @staticmethod
    @abstractmethod
    def read_name_index():
        """Reads name index as Index

        Returns:
            NameIndex
        """
        pass

    @abstractmethod
    def write_name_index(self):
        """Writes name index to a file

        Returns:
            Index
        """
        pass

    def update_name_index(self, item, write=True):
        self.name_index.update(item)
        if write:
            self.write_name_index()

    def add_name_proposal(self, item):
        """Adds one item to name proposals"""
        if item.form is None:
            raise ValueError('Item needs a form before it can be added to name proposals')
        if item.name is None:
            raise ValueError('Item needs a name before it can be added to name proposals')
        manufacturer, _ = self.manufacturers.find(item.name)
        model = self.manufacturers.model(item.name)
        if not (
                (self.name_proposals['form'] == item.form)
                & (self.name_proposals['manufacturer'] == manufacturer)
                & (self.name_proposals['model'] == model)
        ).any():
            self.name_proposals.loc[len(self.name_proposals.index)] = [item.form, manufacturer, model]

    def init_name_proposals(self):
        """Gets name proposals for new measurements

        Returns:
            NameIndex
        """
        name_proposals = NameIndex()
        for db in ['crinacle', 'oratory1990', 'rtings']:
            name_index = NameIndex.read_tsv(os.path.join(DIR_PATH, db, 'name_index.tsv'))
            name_proposals.concat(name_index)
        # TODO: Name indexes for Innerfidelity and headphonecom
        # for db in ['innerfidelity', 'headphonecom']:
        #     name_index = NameIndex.read_files(os.path.join(DIR_PATH, db, 'data', '**', '*.csv'))
        #     name_proposals.concat(name_index)

        proposal_data = {'form': [], 'manufacturer': [], 'model': []}
        for item in name_proposals.items:
            if not item.name or item.form == 'ignore':
                continue
            manufacturer, match = self.manufacturers.find(item.name)
            if not manufacturer:
                continue
            proposal_data['form'].append(item.form)
            proposal_data['manufacturer'].append(manufacturer)
            proposal_data['model'].append(self.manufacturers.model(item.name))
        self.name_proposals = pd.DataFrame(proposal_data)
        self.name_proposals.drop_duplicates(inplace=True)

    def get_name_proposals(self, source_name, n=4, normalize_digits=False, normalize_extras=False, threshold=60):
        """Prompts manufacturer, model and form from the user

        Args:
            source_name: Name as it exists in the measurement source
            n: Number of proposals to return
            normalize_digits: Normalize all digits to zeros before calculating fuzzy string matching score
            normalize_extras: Remove extra details in the parentheses
            threshold: Score threshold

        Returns:
            NameItem
        """
        def fuzzy(fn, a, b):
            a = a.lower()
            b = b.lower()
            if normalize_digits:
                a = re.sub(r'\d', '0', a).strip()
                b = re.sub(r'\d', '0', b).strip()
            if normalize_extras:
                a = re.sub(r'\(.+\)$', '', a).strip()
                b = re.sub(r'\(.+\)$', '', b).strip()
            return fn(a, b)

        manufacturer, manufacturer_match = self.manufacturers.find(source_name)
        if not manufacturer:
            return NameIndex([])
        false_model = self.manufacturers.model(source_name)
        # Select only the items with the same manufacturer
        models = self.name_proposals[self.name_proposals.manufacturer == manufacturer]

        # Calculate ratios
        partial_ratios = [fuzzy(fuzz.partial_ratio, model, false_model) for model in models.model.tolist()]
        ratios = [fuzzy(fuzz.ratio, model, false_model) for model in models.model.tolist()]

        models = models.assign(partial_ratio=partial_ratios)
        models = models.assign(ratio=ratios)
        models.sort_values('ratio', ascending=False, inplace=True)
        models = models[models.partial_ratio >= threshold]
        proposals = []
        for i, row in models.iterrows():
            proposals.append(NameItem(None, f'{manufacturer} {row.model}', row.form))
        ni = NameIndex(items=proposals[:n])
        return ni

    def guess_name(self, item):
        """Tries to guess what the name might be."""
        return item.name or item.source_name or re.sub(
            r'\.(?:txt|csv)$', '', item.url.split('/')[-1], flags=re.IGNORECASE)

    def resolve(self, item):
        """Resolve name for a single item. Updates the item in place.

        Args:
            item: The crawl index NameItem to resolve

        Returns:
            None
        """
        ground_truth = self.name_index.find_one(url=item.url)
        if ground_truth and ground_truth.rig is not None:
            if ground_truth.name is not None:
                item.name = ground_truth.name
            if ground_truth.source_name is not None:
                item.source_name = ground_truth.source_name
            if ground_truth.form is not None:
                item.form = ground_truth.form
            if ground_truth.rig is not None:
                item.rig = ground_truth.rig

    def prompt_callback(self, prompted_item):
        if prompted_item.form != 'ignore':
            if not is_file_name_allowed(prompted_item.name):
                raise ValueError(f'Name "{prompted_item.name}" doesn\'t work as file name')
            self.active_list_item.resolution = 'success'
            # Replace manufacturer name with the true manufacturer name
            manufacturer, manufacturer_match = self.manufacturers.find(prompted_item.name, ignore_case=True)
            if manufacturer is None:
                raise UnknownManufacturerError(f'Manufacturer is not known for "{prompted_item.name}"')
            prompted_item.name = self.manufacturers.replace(prompted_item.name)
            self.add_name_proposal(prompted_item)
        else:
            self.active_list_item.resolution = 'ignore'
        self.update_name_index(prompted_item, write=True)
        if (
                self.delete_existing_on_prompt
                and self.target_path(prompted_item) is not None
                and self.target_path(prompted_item).exists()
        ):
            self.target_path(prompted_item).unlink()
        self.next_prompt()

    def create_prompt_callback(self, *args, **kwargs):
        return self.prompt_callback

    def is_prompt_needed(self, item):
        if item.form == 'ignore':
            return False
        return item.name is None or item.form is None

    def create_prompts(self, max_prompts=30):
        if self.name_proposals is None:
            self.init_name_proposals()
        self.prompts = []
        crawled_items = sorted(self.crawl_index.items, key=lambda item: item.source_name or item.url.split('/')[-1])
        for item in crawled_items:
            if not self.is_prompt_needed(item):
                continue
            self.prompts.append(PromptListItem(NamePrompt(item, self.prompt_callback), self.switch_prompt))
            if len(self.prompts) >= max_prompts:
                break

    # Crawler methods

    def download(self, url, file_path):
        """Downloads a file from a URL

        Args:
            url: URL to download
            file_path: File path for the downloaded file

        Returns:
            Bool depicting if download succeeded or not
        """
        file_path = Path(file_path)
        if file_path.exists() and not self.redownload:
            with open(file_path, 'rb') as fh:
                return fh.read()
        file_path.parent.mkdir(exist_ok=True, parents=True)
        res = requests.get(url, stream=True)
        if res.status_code < 200 or res.status_code >= 300:
            raise Exception(f'Failed to download "{url}"')  # TODO: Exception type
        with open(file_path, 'wb') as fh:
            res.raw.decode_content = True
            shutil.copyfileobj(res.raw, fh)
        print('Downloaded to "{}"'.format(file_path))
        return res.raw

    def get_beautiful_soup(self, url):
        self.driver.get(url)
        sleep(2)  # Giving some time for Selenium to render the page
        html = self.driver.find_element(By.TAG_NAME, 'html').get_attribute('outerHTML')
        return BeautifulSoup(html, 'html.parser')

    @abstractmethod
    def crawl(self):
        """Crawls measurement URLs

        Returns:
            NameIndex with the crawled items
        """
        pass

    # Processing methods

    @abstractmethod
    def target_group_key(self, item):
        """Key for grouping measurements (NameItems) that should be averaged

        Args:
            item: NameItem

        Returns:
            Group key, None if necessary props are missing
        """

    @abstractmethod
    def target_path(self, item):
        """Target file path for the item in measurements directory

        Args:
            item: NameItem for the measurement

        Returns:
            Target file path, None if necessary props are missing
        """

    @abstractmethod
    def process_group(self, items, new_only=True):
        """Processes measurements for a single unit

        Args:
            items: NameItem to be processed together
            new_only: Only process measurements that don't exist yet?

        Returns:
            None
        """
        pass

    def process(self, new_only=True):
        self.name_index = self.read_name_index()
        groups = {}
        for item in self.name_index.items:
            if item.form == 'ignore':
                continue
            key = self.target_group_key(item)
            if key is None:
                continue
            if key not in groups:
                groups[key] = []
            groups[key].append(item)
        for items in groups.values():
            self.process_group(items, new_only=new_only)

    # UI methods

    def reload_ui(self):
        """Refreshes IPyWidgets UI"""
        self.prompt_list = widgets.VBox(
            [prompt.widget for prompt in self.prompts],
            layout=widgets.Layout(max_height='600px', overflow='auto', width='324px'))
        children = [self.prompt_list]
        if self.active_list_item:
            children.append(self.active_list_item.name_prompt.widget)
        self.widget.children = children

    def switch_prompt(self, new_active):
        """Switches to a different item in the prompt list in the IPyWidgets UI"""
        for i, prompt_list_item in enumerate(self.prompts):
            prompt_list_item.inactive_style()
        self.active_list_item = new_active
        if self.active_list_item and self.active_list_item.widget.button_style not in ['success', 'ignore']:  # Selected something i.e. is not clear out in the end of the list
            self.active_list_item.active_style()
            # Item needs to be resolved and updated first
            self.resolve(self.active_list_item.name_prompt.item)
            if self.active_list_item.name_prompt.item.form == 'ignore':
                self.prompt_callback(self.active_list_item.name_prompt.item)
                self.reload_ui()
                return
            # then rest of the optional properties
            self.active_list_item.name_prompt.guessed_name = self.guess_name(self.active_list_item.name_prompt.item)
            self.active_list_item.name_prompt.name_proposals = self.get_name_proposals(
                self.active_list_item.name_prompt.guessed_name, n=4, threshold=10)
            self.active_list_item.name_prompt.similar_names = [
                    item.name for item in self.get_name_proposals(
                        self.active_list_item.name_prompt.guessed_name, n=6, normalize_digits=True,
                        normalize_extras=True, threshold=0).items]
            if self.active_list_item.name_prompt.item.name is not None and self.active_list_item.name_prompt.item.form is not None:
                # Prompting can resolve AutoEq name and the headphone form, both got resolved here, no need to prompt
                self.prompt_callback(self.active_list_item.name_prompt.item)
        self.reload_ui()

    def next_prompt(self):
        """Switches to next prompt in the IPyWidgets UI"""
        ix = self.prompts.index(self.active_list_item)
        self.switch_prompt(self.prompts[ix + 1] if ix + 1 < len(self.prompts) else None)

    # Control flow

    def run(self):
        self.crawl()
        self.create_prompts()
        self.reload_ui()
        # Crawler.process_all() needs to be invoked after user has resolved prompts
