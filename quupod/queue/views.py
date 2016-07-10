"""All queue-related views."""

from .forms import InquiryForm
from .forms import PromotionForm
from .logic import get_inquiry_for_asker
from .logic import maybe_promote_current_user
from .logic import update_context_with_queue_config

from flask import abort
from flask import Blueprint
from flask import g
from flask import redirect
from flask import request
from quupod.forms import choicify
from quupod.models import Inquiry
from quupod.models import Participant
from quupod.models import User
from quupod.models import Queue
from quupod.models import QueueRole
from quupod.utils import emitQueueInfo
from quupod.utils import emitQueuePositions
from quupod.views import current_user
from quupod.views import render
from quupod.views import url_for

import flask_login

queue = Blueprint(
    'queue',
    __name__,
    url_prefix='/<string:queue_url>',
    template_folder='templates')


@queue.url_defaults
def add_queue_url(endpoint: str, values: dict) -> None:
    """Add information to every URL build."""
    values.setdefault('queue_url', getattr(g, 'queue_url', None))


@queue.url_value_preprocessor
def pull_queue_url(endpoint: str, values: dict) -> None:
    """Extract information from the queue URL."""
    g.queue_url = values.pop('queue_url')
    g.queue = Queue.query.filter_by(url=g.queue_url).one_or_none()
    if not g.queue:
        abort(404)


def render_queue(template: str, *args, **context) -> str:
    """Special rendering for queue."""
    maybe_promote_current_user()
    update_context_with_queue_config(context)
    context.setdefault('queue', g.queue)
    return render(template, *args, **context)


#########
# QUEUE #
#########


@queue.route('/')
def home() -> str:
    """List all unresolved inquiries for the homepage."""
    if current_user().can('help'):
        return redirect(url_for('admin.home'))
    return render_queue(
        'landing.html',
        num_inquiries=Inquiry.get_num_unresolved(),
        ttr=g.queue.ttr())


# TODO cleanup
@queue.route('/promote/<string:role_name>', methods=['POST', 'GET'])
@queue.route('/promote')
def promote(role_name: str=None) -> str:
    """Promote the user accessing this page."""
    if not current_user().is_authenticated:
        return render_queue(
            'error.html',
            code='Oops',
            message='You need to be logged in to promote an account!')
    part = Participant.query.filter_by(
        queue_id=g.queue.id,
        user_id=current_user().id).one_or_none()
    n_owners = Participant.query.join(QueueRole).filter(
        QueueRole.name == 'Owner',
        Participant.queue_id == g.queue.id).count()
    if part and part.role.name == 'Owner' and n_owners <= 1:
        return render_queue(
            'error.html',
            code='Oops',
            message='You cannot demote yourself from owner until another owner'
            ' has been added.')
    promotion_setting = g.queue.setting(name='self_promotion', default=None)
    if not promotion_setting or not promotion_setting.enabled:
        abort(404)
    tuples = [s.split(':') for s in promotion_setting.value.splitlines()]
    codes = dict((k.strip().lower(), v.strip()) for k, v in tuples)
    role_names = [role.name for role in g.queue.roles]
    if n_owners == 0:
        role_name = 'Owner'
    roles = [s.lower() for s in role_names + list(codes.keys())]
    if role_name and role_name.lower() not in roles:
        abort(404)
    if not role_name:
        return render_queue(
            'roles.html',
            title='Promotion Form',
            message='Welcome. Please select a role below.',
            roles=[name for name in role_names if name.lower() in codes])
    if n_owners == 0:
        code = '*'
    elif role_name.lower() not in codes:
        abort(404)
    else:
        code = codes[role_name.lower()]
    form = PromotionForm(request.form)
    if request.method == 'POST' or code == '*':
        if not (code == '*' or request.form['code'] == code):
            form.errors.setdefault('code', []).append('Incorrect code.')
            return render_queue(
                'form.html',
                form=form,
                submit='Promote',
                back=url_for('queue.promote'))
        role = QueueRole.query.filter_by(
            name=role_name, queue_id=g.queue.id).one()
        part = Participant.query.filter_by(
            user_id=current_user().id,
            queue_id=g.queue.id).one_or_none()
        if part:
            part.update(role_id=role.id).save()
        else:
            Participant(
                user_id=current_user().id,
                queue_id=g.queue.id,
                role_id=role.id
            ).save()
        return render_queue(
            'confirm.html',
            title='Promotion Success',
            message='You have been promoted to %s' % role_name,
            action='Onward',
            url=url_for('admin.home'))
    return render_queue(
        'form.html',
        form=form,
        submit='Promote',
        back=url_for('queue.promote'))

########
# FLOW #
########


# TODO cleanup
@queue.route('/request', methods=['POST', 'GET'])
def inquiry() -> str:
    """Place a new request.

    This request which may be authored by either a system user or an anonymous
    user.
    """
    user, form = flask_login.current_user, InquiryForm(request.form)
    if user.is_authenticated:
        form = InquiryForm(request.form, obj=user)
    elif g.queue.setting(name='require_login').enabled:
        return render_queue(
            'confirm.html',
            title='Login Required',
            message='Login to add an inquiry, and start using this queue.')
    n = int(g.queue.setting(name='max_requests').value)
    filter_id = User.email == current_user().email \
        if current_user().is_authenticated \
        else User.name == request.form.get('name', None)
    not_logged_in_max = ''
    if Inquiry.query.join(User).filter(
            filter_id,
            Inquiry.status == 'unresolved',
            Inquiry.queue_id == g.queue.id).count() >= n:
        if not current_user().is_authenticated:
            not_logged_in_max = 'If you haven\'t submitted a request, try'
            ' logging in and re-requesting.'
        return render_queue(
            'confirm.html',
            title='Oops',
            message='Looks like you\'ve reached the maximum number of times '
            'you can add yourself to the queue at once (<code>%d</code>). '
            '%s' % (
                n,
                not_logged_in_max or
                'Would you like to cancel your oldest request?'),
            action='Cancel Oldest Request',
            url=url_for('queue.cancel'))
    form.location.choices = choicify(
        g.queue.setting('locations').value.split(','))
    form.category.choices = choicify(
        g.queue.setting('inquiry_types').value.split(','))
    if request.method == 'POST' and form.validate() and \
            g.queue.is_valid_assignment(request, form):
        inquiry = Inquiry(**request.form)
        inquiry.queue_id = g.queue.id
        if current_user().is_authenticated:
            inquiry.owner_id = current_user().id
        inquiry.save()
        emitQueueInfo(g.queue)
        return redirect(url_for('queue.waiting', inquiry_id=inquiry.id))
    return render_queue(
        'form.html',
        form=form,
        title='Request Help',
        submit='Request Help')


@queue.route('/cancel/<int:inquiry_id>')
@queue.route('/cancel')
def cancel(inquiry_id: int=None) -> str:
    """Cancel placed request."""
    inquiry = get_inquiry_for_asker()
    if inquiry.is_owned_by_current_user():
        inquiry.close()
    else:
        return render_queue(
            'error.html',
            code='404',
            message='You cannot cancel another user\'s request. This incident'
            ' has been logged.',
            url=url_for('queue.home'),
            action='Back Home')
    emitQueuePositions(inquiry)
    emitQueueInfo(inquiry.queue)
    return redirect(url_for('queue.home'))


@queue.route('/waiting/<int:inquiry_id>')
@queue.route('/waiting')
def waiting(inquiry_id: int=None) -> str:
    """Screen shown after user has placed request and is waiting."""
    inquiry = get_inquiry_for_asker()
    return render_queue(
        'waiting.html',
        position=inquiry.current_position(),
        group=inquiry.get_similar_inquiries(),
        inquiry=inquiry,
        details='Location: %s, Assignment: %s, Problem: %s, Request: %s' % (
            inquiry.location,
            inquiry.assignment,
            inquiry.problem,
            inquiry.created_at.humanize()))


################
# LOGIN/LOGOUT #
################


@queue.route('/login', methods=['POST', 'GET'])
def login() -> str:
    """Login using globally defined login procedure."""
    from quupod.public.views import login
    return login(
        home=url_for('queue.home', _external=True),
        login=url_for('queue.login', _external=True))


@queue.route('/logout')
def logout() -> str:
    """Logout using globally defined logout procedure."""
    from quupod.public.views import logout
    return logout(home=url_for('queue.home', _external=True))
