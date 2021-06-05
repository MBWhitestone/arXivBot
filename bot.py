#!/usr/bin/env python3
"""File: bot.py

This file implements arΧivBot, a Discord arXiv bot.
License: MIT.
MBWhitestone, 2021.
"""

import os
import logging
import asyncio
import argparse

import arxiv
import discord as dc

from zlib import adler32
from ruamel.yaml import YAML


async def get_papers(cat, q, sort_by, n=3, iterative=True):
    """Retrieve papers from arXiv.

    Args:
        cat (str): arXiv category
        q (str): query
        sort_by (str, optional): sorting order.
        n (int, optional): number of results. Defaults to 3.
        iterative (bool, optional): make iterator. Defaults to False.

    Returns:
        list / iterator: container with hashmap for each paper.
    """
    return arxiv.Search(query=f"cat:{cat} AND all:{q}",
                        sort_by=arxiv.arxiv.SortCriterion[sort_by],
                        max_results=n).get()


async def get_channel(client, channel):
    """Return channel as Discord object.

    Args:
        client (dc.Client): Discord Client object.
        channel (str, optional): Channel name.

    Raises:
        ValueError: if value of channel is incorrect.

    Returns:
        dc channel object: discord channel
    """
    for c in client.get_all_channels():
        if c.name == channel:
            return c
    raise ValueError(f'Unknown channel {channel}')


async def chop_str(to_chop, chop=1024, remove_whitespace=True):
    """Chop a string at maximum length chop, optionally remove \n ∧ \t."""
    if remove_whitespace:
        to_chop = to_chop.replace('\n', ' ').replace('\t', ' ')
    s = to_chop[:chop - 1] + '…' if len(to_chop) > chop else to_chop
    return s


async def embed_paper(paper, summary_len, color):
    """Create paper embedding from dictionary.

    Limits are based on
        https://discord.com/developers/docs/resources/channel#embed-limits.

    Args:
        paper (dict): arXiv API paper.
        summary_len (int, optional): maximum length of summary.
        color (hex, optional): message color, if None based on query.

    Returns:
        dc.Embed: Discord embedding.
    """
    # Base color on category and query when not specified.
    if color is None:
        color = adler32(bytes(paper.comment, 'utf-8')) % 0xffffff

    emb = dc.Embed(title=await chop_str(paper.title, 256),
                   description=await chop_str(paper.summary, summary_len),
                   type="rich",
                   url=await chop_str(paper.pdf_url),
                   timestamp=paper.updated,
                   color=color)
    emb.set_footer(text=await chop_str(paper.comment))
    emb.set_author(name=await chop_str(', '.join(map(str, paper.authors)),
                                       256))
    return emb


async def is_valid_category(category):
    """Returns whether category (str) is a valid arXiv category."""
    return await is_acm(category) or await is_msc(category)


async def is_msc(msc):
    """Returns whether msc is a valid msc category.

    See also:
    https://mathscinet.ams.org/mathscinet/msc/pdfs/classifications2020.pdf
    """
    return len(msc) == 5 and msc[:2].isdigit() and msc[3:5].isdigit()


async def is_acm(acm):
    """Returns whether acm (str) is a valid acm category."""
    return 1 < len(acm) < 6 and '.' in acm


def transform_config(cfg, split_1='search:', split_2='known_papers:'):
    """Ugly function to make cfg.yml less ugly."""
    before_search, after_search = cfg.split(split_1, 1)
    search_default, papers_default = after_search.split(split_2, 1)

    search, paper_comment = '', ''
    for line in search_default.splitlines():
        line = line.strip()
        if line:
            if line.startswith('-'):
                search += '  '
            elif line.startswith('# List of paper ids'):
                paper_comment = line
                continue
            search += '  ' + line + '\n'

    ok = papers_default
    if '-' in papers_default:
        ok = ' ['
        for line in papers_default.splitlines():
            line = line.strip()
            if '-' in line:
                ok += line.split('- ')[1] + ', '
        ok = ok[:-2] + ']'

    return f"{before_search}{split_1}\n{search}{paper_comment}\n{split_2}{ok}"


class arΧivBot(dc.Client):
    """The one and only arΧiv Discord bot."""

    def __init__(self, cfg_file='cfg.yml'):
        """Initialize the arΧivBot from a configuration YAML.

        Args:
            cfg_file (str, optional): Config YAML. Defaults to 'cfg.yml'.
        """
        dir_path = os.path.dirname(os.path.realpath(__file__))
        self.cfg_path = os.path.join(dir_path, cfg_file)
        self.yaml = YAML()
        # Some values to keep the .yml in shape.
        self.yaml.indent(mapping=15, sequence=4, offset=2)

        self.cfg = self._cfg_from_disk()
        for key, value in self.cfg.items():
            if key == 'known_papers':
                if value is None:
                    self.cfg['known_papers'] = []
                    value = []
                value = f'contains {len(value)} papers'
            if key == 'search':
                for category, query in value.items():
                    logging.info(f"\tSearch {category}: {query}.")
            else:
                logging.info(f"\t{key}: {value}")
        self.cfg['hotword'] = self.cfg['hotword'].lower()
        self.cfg['sort_by'] = (self.cfg['sort_by'][0].capitalize() +
                               self.cfg['sort_by'][1:])

        self.key = self._get_key()
        self.prompts = ['add', 'del', 'set', 'list']
        self.hidden_keys = ['search', 'key', 'known_papers']
        self.search_key = "search"

        super().__init__()

    def _cfg_from_disk(self):
        """Read and return configuration file from disk."""
        with open(self.cfg_path, 'r') as f:
            cfg = self.yaml.load(f)
        return cfg

    async def _cfg_to_disk(self, default_flow_style=False):
        """Write current configuration to disk."""
        self.yaml.default_flow_style = default_flow_style
        with open(self.cfg_path, 'w') as f:
            self.yaml.dump(self.cfg, f, transform=transform_config)

    def _get_key(self):
        """Return Discord bot key."""
        try:
            import key
            key = key.key
        except ImportError:
            try:
                key = self.cfg['key']
            except KeyError:
                key = None

        if key is None:
            raise KeyError('Specify a Discord bot key in cfg.yml or key.py')
        return key

    async def on_ready(self):
        """Start the bot when ready."""
        logging.info(f'\n\tLogged in as {self.user.name} ({self.user.id})\n')
        self.loop.create_task(self.check_arxiv())

    async def on_message(self, message):
        """Defines what to do when a new message enters a Discord channel.

        Args:
            message (str): Discord message.
        """
        if not message.content.lower().startswith(self.cfg['hotword']):
            return

        logging.info(f'Got message {await chop_str(message.content)}')
        channel = message.channel
        content = message.content.split(" ")
        n_content = len(content)
        if n_content >= 2:
            prompt = content[1].lower()

            if prompt == self.prompts[0] and n_content >= 4:
                await self._add(channel, *content[2:])
            elif prompt == self.prompts[1] and n_content >= 4:
                await self._del(channel, *content[2:])
            elif prompt == self.prompts[2] and n_content == 4:
                await self._set(channel, content[2], content[3])
            elif prompt == self.prompts[3]:
                await self._list(channel)
            else:
                await self._help(channel)
        else:
            await self._help(channel)

    async def process_paper(self, channel, paper):
        """Returns whether a new paper is processed.

        Sends paper to Discord if not yet processed.

        Args:
            channel (dc.channel): Discord channel.
            paper (dict): arXiv paper information.

        Returns:
            bool: whether a new paper is processed.
        """
        paper_id, new_paper = paper.entry_id.split('/')[-1], False
        if paper_id not in self.cfg['known_papers']:
            new_paper = True
            logging.info(f'Got new paper {await chop_str(paper.title, 69)}')
            emb = await embed_paper(paper, self.cfg['summary_length'],
                                    self.cfg['message_color'])
            await channel.send(embed=emb)
            self.cfg['known_papers'].append(paper_id)

        return new_paper

    async def check_arxiv(self):
        """Main function to keep the bot informed on arXiv papers."""
        await self.wait_until_ready()
        channel = await get_channel(self, self.cfg['paper_channel'])

        while not self.is_closed():
            new_paper = False
            # Search for new papers.
            for category, queries in self.cfg["search"].items():
                for query in queries:
                    papers = await get_papers(cat=category, q=query,
                                              sort_by=self.cfg['sort_by'],
                                              n=self.cfg['n_query'])
                    for paper in papers:
                        # Don't need the comment field; let's use it.
                        paper.comment = category + ': ' + query
                        if await self.process_paper(channel, paper):
                            new_paper = True

            if new_paper:
                await self._cfg_to_disk()
            logging.info(f"Sleeping {self.cfg['query_interval']} seconds now…")
            await asyncio.sleep(self.cfg['query_interval'])

    async def _add(self, channel, category, *query):
        """Add a search query to the search list.

        Args:
            channel (discord.Message.channel): Discord channel to report.
            category (str): Query arXiv category.
            query (list(str)): the actual query.
        """
        if await is_valid_category(category):
            keys = [k.lower() for k in self.cfg[self.search_key].keys()]
            if category.lower() not in keys:
                self.cfg[self.search_key][category] = []
                await channel.send(f"Added {category} to the search list.")

            query = ' '.join(query)
            if query and query not in self.cfg[self.search_key][category]:
                self.cfg[self.search_key][category].append(query)
                m = f"Added {query} to the search for {category}."
            else:
                m = f"Query {query} for **{category}** already known or empty."
            await channel.send(m)
            await self._cfg_to_disk()
        else:
            await channel.send(f'{category} is not a valid arXiv category.')

    async def _del(self, channel, category, *query):
        """Delete a query from the search list.

        Args:
            channel (discord.Message.channel): Discord channel to report.
            category (str): Query arXiv category.
            query (list(str)): the actual query.
        """
        if (await is_valid_category(category) and
                category in [k for k in self.cfg[self.search_key].keys()]):
            query = ' '.join(query)
            if query in self.cfg[self.search_key][category]:
                self.cfg[self.search_key][category].remove(query)
                m = f"Query **{query}** "
                if not self.cfg[self.search_key][category]:
                    del self.cfg[self.search_key][category]
                    m += f"and category **{category}** "
                m += "removed from the search list."
            else:
                m = f"Query **{category}: {query}** is not in the search."
            await channel.send(m)
            await self._cfg_to_disk()
        else:
            m = f'Category **{category}** cannot be in the arXiv search list.'
            await channel.send(m)

    async def _set(self, channel, key, value):
        """Set a configuration parameter of the bot.

        Args:
            channel (discord.Message.channel): Discord channel to report.
            key (str): configuration parameter.
            value (str): parameter value.
        """
        key = key.lower()
        if key in self.hidden_keys or key not in self.cfg.keys():
            await channel.send(f'{key} is not available for setting {value}.')
            return

        valid = False
        if key == "paper_channel":
            pot_channel = await get_channel(self, value)
            if pot_channel is not None:
                valid = True
        elif (key == "summary_lenght" and value.isdigit() and
              int(value) <= 2048):
            valid, value = True, int(value)
        elif key == "n_query" and value.isdigit() and 0 < int(value) < 1000:
            valid, value = True, int(value)
        elif key == "sort_by":
            options = ['relevance', 'lastUpdatedDate', 'submittedDate']
            for o in options:
                if o.lower() == value.lower():
                    valid = True
                    value = o
                    break
        elif key == "message_color" and value.isdigit() and int(value) > 0:
            valid, value = True, int(value)
        elif (key == "query_interval" and value.isdigit() and
              29 < int(value) < 6000000):
            valid, value = True, int(value)
        elif key == "hotword" and 2 < len(value) < 16:
            valid, value = True, value.lower()

        if valid:
            self.cfg[key] = value
            await self._cfg_to_disk()
            await channel.send(f'Key **{key}** is set to value **{value}**.')
        else:
            await channel.send(f'Invalid option **{value}** for **{key}**.')

    async def _list(self, channel):
        """Sends an overview of known papers and search queries to channel.

        Args:
            channel (discord.Message.channel): Discord channel to report.
        """
        s = await self._repr_queries()
        chop = await chop_str(str(self.cfg['known_papers']))
        s += f"**Known papers ({len(self.cfg['known_papers'])}):**\n> {chop}"
        await channel.send(s)

    async def _repr_queries(self):
        """Returns a string representation of the search list."""
        s = '**Search queries:**\n'
        for category, query in self.cfg['search'].items():
            s += f"> {category}: {query}\n"
        return await chop_str(s, remove_whitespace=False)

    async def _repr_parameters(self, ):
        """Returns a string representation of configuration parameters."""
        s = '**Configuration:**\n'
        for param, value in self.cfg.items():
            if param not in self.hidden_keys:
                s += f"> *{param}:*     {value}\n"
        return s

    async def _help(self, channel):
        """Send help message with orange color for arXivBot to channel."""
        s = "**Usage:**\n" \
            + "```!arxiv add <category:required> <query:required>\n" \
            + "!arxiv del <key:required> <value:required>\n" \
            + "!arxiv set <key:required> <value:required>\n" \
            + "!arxiv list\n" \
            + "!arxiv help```"
        s += await self._repr_queries()
        s += await self._repr_parameters()
        emb = dc.Embed(title="arXiv Discord bot",
                       description=s,
                       type="rich",
                       url="https://github.com/MBWhitestone/arXivBot",
                       color=0xffa500)
        await channel.send(embed=emb)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='The arΧivBot script.')
    p.add_argument('-q', '--quiet', action='store_true', help='no logging')
    p.add_argument('-c', '--config', default='cfg.yml', help='config file')
    args = p.parse_args()
    if not args.quiet:
        logging.basicConfig(level=logging.INFO)
    logging.info('Starting arXiv Discord bot…')

    client = arΧivBot(cfg_file=args.config)
    client.run(client.key)
