'''
    Copyright (C) 2017 Gitcoin Core

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program. If not, see <http://www.gnu.org/licenses/>.

'''
import json
import pprint
from enum import Enum

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.http import Http404, JsonResponse
from django.utils import timezone

import requests
from bs4 import BeautifulSoup
from dashboard.models import Bounty, BountySyncRequest
from dashboard.notifications import (
    maybe_market_to_email, maybe_market_to_github, maybe_market_to_slack, maybe_market_to_twitter,
)
from economy.utils import convert_amount
from ratelimit.decorators import ratelimit


# gets amount of remote html doc (github issue)
@ratelimit(key='ip', rate='100/m', method=ratelimit.UNSAFE, block=True)
def amount(request):
    response = {}

    try:
        amount = request.GET.get('amount')
        deonomination = request.GET.get('denomination', 'ETH')
        if deonomination == 'ETH':
            amount_in_eth = float(amount)
        else:
            amount_in_eth = convert_amount(amount, deonomination, 'ETH')
        amount_in_usdt = convert_amount(amount_in_eth, 'ETH', 'USDT')
        response = {
            'eth': amount_in_eth,
            'usdt': amount_in_usdt,
        }
        return JsonResponse(response)
    except Exception as e:
        print(e)
        raise Http404



# gets title of remote html doc (github issue)
@ratelimit(key='ip', rate='10/m', method=ratelimit.UNSAFE, block=True)
def title(request):
    response = {}

    url = request.GET.get('url')
    urlVal = URLValidator()
    try:
        urlVal(url)
    except ValidationError:
        response['message'] = 'invalid arguments'
        return JsonResponse(response)

    if url.lower()[:19] != 'https://github.com/':
        response['message'] = 'invalid arguments'
        return JsonResponse(response)

    try:
        html_response = requests.get(url)
    except ValidationError:
        response['message'] = 'could not pull back remote response'
        return JsonResponse(response)

    title = None
    try:
        soup = BeautifulSoup(html_response.text, 'html.parser')

        eles = soup.findAll("span", { "class" : "js-issue-title" })
        if len(eles):
            title = eles[0].text

        if not title and soup.title:
            title = soup.title.text

        if not title:
            for link in soup.find_all('h1'):
                print(link.text)

    except ValidationError:
        response['message'] = 'could not parse html'
        return JsonResponse(response)

    try:
        response['title'] = title.replace('\n', '').strip()
    except Exception as e:
        print(e)
        response['message'] = 'could not find a title'

    return JsonResponse(response)


# gets description of remote html doc (github issue)
@ratelimit(key='ip', rate='10/m', method=ratelimit.UNSAFE, block=True)
def description(request):
    response = {}

    url = request.GET.get('url')
    urlVal = URLValidator()
    try:
        urlVal(url)
    except ValidationError as e:
        response['message'] = 'invalid arguments'
        return JsonResponse(response)

    if url.lower()[:19] != 'https://github.com/':
        response['message'] = 'invalid arguments'
        return JsonResponse(response)

    # Web format:  https://github.com/jasonrhaas/slackcloud/issues/1
    # API format:  https://api.github.com/repos/jasonrhaas/slackcloud/issues/1
    gh_api = url.replace('github.com', 'api.github.com/repos')

    try:
        api_response = requests.get(gh_api)
    except ValidationError as e:
        response['message'] = 'could not pull back remote response'
        return JsonResponse(response)

    if api_response.status_code != 200:
        response['message'] = 'there was a problem reaching the github api'
        return JsonResponse(response)

    try:
        body = api_response.json()['body']
    except ValueError as e:
        response['message'] = e
    except KeyError as e:
        response['message'] = e
    else:
        response['description'] = body.replace('\n', '').strip()

    return JsonResponse(response)

# gets keywords of remote issue (github issue)
@ratelimit(key='ip', rate='10/m', method=ratelimit.UNSAFE, block=True)
def keywords(request):
    response = {}
    keywords = []

    url = request.GET.get('url')
    urlVal = URLValidator()
    try:
        urlVal(url)
    except ValidationError:
        response['message'] = 'invalid arguments'
        return JsonResponse(response)

    if url.lower()[:19] != 'https://github.com/':
        response['message'] = 'invalid arguments'
        return JsonResponse(response)

    try:
        repo_url = None
        if '/pull' in url:
            repo_url = url.split('/pull')[0]
        if '/issue' in url:
            repo_url = url.split('/issue')[0]
        split_repo_url = repo_url.split('/')
        keywords.append(split_repo_url[-1])
        keywords.append(split_repo_url[-2])

        html_response = requests.get(repo_url)
    except ValidationError:
        response['message'] = 'could not pull back remote response'
        return JsonResponse(response)
    except AttributeError:
        response['message'] = 'could not pull back remote response'
        return JsonResponse(response)

    try:
        soup = BeautifulSoup(html_response.text, 'html.parser')

        eles = soup.findAll("span", {"class" : "lang"})
        for ele in eles:
            keywords.append(ele.text)

    except ValidationError:
        response['message'] = 'could not parse html'
        return JsonResponse(response)

    try:
        response['keywords'] = keywords
    except Exception as e:
        print(e)
        response['message'] = 'could not find a title'

    return JsonResponse(response)


def normalizeURL(url):
    if url[-1] == '/':
        url = url[0:-1]
    return url

# returns didChange if bounty has changed since last sync
# then old_bounty
# then new_bounty
def syncBountywithWeb3(bountyContract, url, network):
    bountydetails = bountyContract.call().bountydetails(url)
    return process_bounty_details(bountydetails, url, bountyContract.address, network)


class BountyStage(Enum):
    """ Python enum class that matches up with the Standard Bounties BountyStage enum.

    Attributes:
        Draft (int): Bounty is a draft.
        Active (int): Bounty is active.
        Dead (int): Bounty is dead.
    """

    Draft = 0
    Active = 1
    Dead = 2


def process_bounty_details(bountydetails, url, contract_address, network):
    url = normalizeURL(url)

    # See Line 303 in bounty_details.js for original object
    bountyId = bountydetails.get('bountyId', {})
    bountyData = bountydetails.get('bountyData', {})
    bountyDataPayload = bountyData.get('payload', {})
    metadata = bountyDataPayload.get('metadata', {})
    bounty = bountydetails.get('bounty', {})
    # Claimee metadata will be empty when bounty is first created
    fulfillments = bountydetails.get('fulfillments', {})

    # Create new bounty (but only if things have changed)
    didChange = False
    old_bounties = Bounty.objects.none()
    try:
        old_bounties = Bounty.objects.filter(
            github_url=url,
            title=bountyDataPayload.get('title'),
            current_bounty=True,
        ).order_by('-created_on')
        didChange = (bountydetails != old_bounties.first().raw_data)
        if not didChange:
            return (didChange, old_bounties.first(), old_bounties.first())
    except Exception as e:
        print('exception in process_bounty_details')
        print(e)
        didChange = True

    # Check if we have any fulfillments.  If so, check if they are accepted.
    # If there are no fulfillments, accepted is automatically False.
    # Currently we are only considering the latest fulfillment.  Std bounties supports multiple.
    fments = fulfillments['fulfillments']
    accepted = fments[-1].get('accepted') if fments else False

    if fments:
        claimeee_address = fments[-1].get('payload', {}).get('fulfiller', {}).get('address', '0x0000000000000000000000000000000000000000')
        claimee_email = fments[-1].get('payload', {}).get('fulfiller', {}).get('email', '')
        claimee_github_username = fments[-1].get('payload', {}).get('fulfiller', {}).get('githubUsername', '')
        claimee_name = fments[-1].get('payload', {}).get('fulfiller', {}).get('name', '')
        claimee_metadata = fments[-1].get('payload', {}).get('metadata', {})
    else:
        claimeee_address = '0x0000000000000000000000000000000000000000'
        claimee_email = ''
        claimee_github_username = ''
        claimee_name = ''
        claimee_metadata = {}

    # Possible Bounty Stages
    # 0: Draft
    # 1: Active
    # 2: Dead
    is_open = True if (bounty.get('bountyStage') == 1 and not accepted) else False

    with transaction.atomic():
        for old_bounty in old_bounties:
            old_bounty.current_bounty = False
            old_bounty.save()
        new_bounty = Bounty.objects.create(
            title=bountyDataPayload.get('title', ''),
            issue_description=bountyDataPayload.get('description', ''),
            web3_created=timezone.datetime.fromtimestamp(bountyDataPayload.get('created')),
            value_in_token=bounty.get('fulfillmentAmount'),
            token_name=bountyDataPayload.get('tokenName', ''),
            token_address=bountyDataPayload.get('tokenAddress', '0x0000000000000000000000000000000000000000'),
            bounty_type=metadata.get('bountyType', ''),
            project_length=metadata.get('projectLength', ''),
            experience_level=metadata.get('experienceLevel', ''),
            github_url=url,  # Could also use payload.get('webReferenceURL')
            bounty_owner_address=bountyDataPayload.get('issuer', {}).get('address', ''),
            bounty_owner_email=bountyDataPayload.get('issuer', {}).get('email', ''),
            bounty_owner_github_username=bountyDataPayload.get('issuer', {}).get('githubUsername', ''),
            bounty_owner_name=bountyDataPayload.get('issuer', {}).get('name', ''),
            # fulfillment_ipfs_hash='',
            is_open=is_open,
            raw_data=bountydetails,
            metadata=metadata,
            current_bounty=True,
            contract_address=contract_address,
            network=network,
            accepted=accepted,
            # These fields are after initial bounty creation, in bounty_details.js
            claimee_metadata=claimee_metadata,
            claimeee_address=claimeee_address,
            claimee_email=claimee_email,
            claimee_github_username=claimee_github_username,
            claimee_name=claimee_name,
            expires_date=timezone.datetime.fromtimestamp(bounty.get('deadline')),
            standard_bounties_id=bountyId,
            balance=bounty.get('balance'),
            num_fulfillments=fulfillments.get('total', 0),
            )
        new_bounty.fetch_issue_item()
        if not new_bounty.avatar_url:
            new_bounty.avatar_url = new_bounty.get_avatar_url()
        new_bounty.save()

    return (didChange, old_bounties.first(), new_bounty)


def process_bounty_changes(old_bounty, new_bounty, txid):

    # process bounty sync requests
    did_bsr = False
    for bsr in BountySyncRequest.objects.filter(processed=False, github_url=new_bounty.github_url):
        did_bsr = True
        bsr.processed = True
        bsr.save()

    # new bounty
    null_address = '0x0000000000000000000000000000000000000000'
    if (old_bounty is None and new_bounty and new_bounty.is_open) or (not old_bounty.is_open and new_bounty.is_open):
        event_name = 'new_bounty'
    elif old_bounty.claimeee_address == null_address and new_bounty.claimeee_address != null_address:
        event_name = 'new_claim'
    elif old_bounty.is_open and not new_bounty.is_open:
        if new_bounty.status == 'dead':
            event_name = 'killed_bounty'
        else:
            event_name = 'approved_claim'
    elif old_bounty.claimeee_address != null_address and new_bounty.claimeee_address == null_address:
        event_name = 'rejected_claim'
    else:
        event_name = 'unknown_event'
    print(event_name)

    # marketing
    print("============ posting ==============")
    did_post_to_twitter = maybe_market_to_twitter(new_bounty, event_name, txid)
    did_post_to_slack = maybe_market_to_slack(new_bounty, event_name, txid)
    did_post_to_github = maybe_market_to_github(new_bounty, event_name, txid)
    did_post_to_email = maybe_market_to_email(new_bounty, event_name, txid)
    print("============ done posting ==============")

    # what happened
    what_happened = {
        'did_bsr': did_bsr,
        'did_post_to_email': did_post_to_email,
        'did_post_to_github': did_post_to_github,
        'did_post_to_slack': did_post_to_slack,
        'did_post_to_twitter': did_post_to_twitter,
    }

    print("changes processed: ")
    pp = pprint.PrettyPrinter(indent=4)
    pp.pprint(what_happened)
