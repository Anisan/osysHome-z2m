from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, IntegerField, SelectField
from wtforms.validators import DataRequired, Optional
from wtforms.widgets import PasswordInput

# Определение класса формы
class SettingsForm(FlaskForm):
    host = StringField('Host', validators=[DataRequired()])
    port = IntegerField('Port', validators=[DataRequired()], default=1883)
    protocol = SelectField(
        'Protocol version',
        choices=[
            ('3.1', 'MQTT 3.1'),
            ('3.1.1', 'MQTT 3.1.1'),
            ('5.0', 'MQTT 5.0'),
        ],
        default='3.1.1',
    )
    login = StringField('Login', validators=[Optional()])
    password = StringField('Password', validators=[Optional()], widget=PasswordInput(hide_value=False))
    topic = StringField('Topic', validators=[DataRequired()])
    submit = SubmitField('Submit')