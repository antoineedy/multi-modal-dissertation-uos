FROM pytorch/pytorch

RUN apt-get update && \
    apt-get install libgl1-mesa-glx -y