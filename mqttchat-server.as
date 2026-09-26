!   mqttchat-server.as

    script MqttChatServer

!! Chat service: answers questions that arrive over MQTT using a local Ollama model.
!!
!! Runs on the PC that holds the GPU, under the AllSpeak Python runtime. It subscribes to one
!! topic, hands every request to the chat plugin, and beats once a tick so the model hands the
!! GPU back the moment video work wants it.

    use mqtt
    use plugin Chat from as_chat.py

    topic RequestTopic
    variable Credentials
    variable Broker
    variable Username
    variable Password
    variable ServiceTopic
    dictionary CredDict
!! @hash 2fa6c6be
!!!

!! Load the MQTT credentials from a local file if present, otherwise fetch them from the chat
!! site's credentials endpoint.
!!
!! This is the same file the browser client reads; the access token is separate, and belongs
!! only on this machine.

    if file `credentials` exists load Credentials from `credentials`
    else
    begin
        get Credentials from url `https://chat.example.com/credentials.php`
            or go to NoCredentials
    end
    put json Credentials into CredDict
    put entry `broker` of CredDict into Broker
    put entry `username` of CredDict into Username
    put entry `password` of CredDict into Password
    put entry `topic` of CredDict into ServiceTopic

    log `Broker is ` cat Broker
    log `Service topic is ` cat ServiceTopic

    if ServiceTopic is empty go to NoTopic
!! @hash e4c6cfd4
!!!

!! Connect, subscribe, and then poll and beat — the whole of the service's own work.
!!
!! The plugin takes each message off the MQTT client itself; the script never touches a
!! payload. That is deliberate. A broker topic receives stray publications sooner or later,
!! and this script has no way to check a value's type before indexing it, so a message it
!! tried to unpack could take the whole service down. The plugin, in Python, can look.
!!
!! The beat is what keeps the GPU available to video work: while the model is resident the
!! plugin watches for video tools and releases the model the moment one wants the GPU.

    init RequestTopic
        name ServiceTopic
        qos 1

    mqtt
        token Username Password
        id uuid
        broker Broker
        port 8883
        subscribe RequestTopic

    while true
    begin
        wait 50 ticks
        chat beat
        chat poll
    end
    stop
!! @hash fcabe35a
!!!

!! The credentials could not be obtained: say so and exit.

NoCredentials:
    print `Failed to get MQTT credentials from the chat site.`
    exit
!! @hash 5d465351
!!!

!! The credentials carry no service topic, so there is nothing to subscribe to.

NoTopic:
    print `The credentials have no topic entry. Add one for the chat service.`
    exit
!! @hash f0c6b076
!!!
